"""在视频结尾追加一段二维码片尾，方便电视观众扫码打开原始链接。

正片不重新编码：只编码几秒钟的片尾，再用 ffmpeg 的 concat demuxer 以 `-c copy`
把两段拼起来，整体开销就是重写一遍文件。为了能无损拼接，片尾的流布局与编码参数
必须与正片一致，所以先用 ffprobe 探测正片，遇到无法对齐的文件（例如 AV1 编码或
带二进制数据流）就跳过并记日志，不影响下载本身。
"""

import json
import os
import re
import subprocess
import tempfile
from urllib.parse import urlparse

FFMPEG_TIMEOUT_SECONDS = 120

# 片尾视频只用 libx264 生成，所以正片必须也是 H.264；音频编码器按正片选择。
SUPPORTED_VIDEO_CODECS = {'h264'}
AUDIO_ENCODERS = {'aac': 'aac', 'opus': 'libopus', 'mp3': 'libmp3lame'}
SUPPORTED_PIXEL_FORMATS = {'yuv420p', 'yuvj420p'}
# mov_text 之外的字幕、以及 bin_data 这类数据流没法在片尾造出同样的轨道
SUPPORTED_SUBTITLE_CODECS = {'mov_text'}

H264_PROFILES = {
    'baseline': 'baseline',
    'constrained baseline': 'baseline',
    'main': 'main',
    'high': 'high',
}

# 拉丁字体用来画链接；提示语是中文，必须另找带中日韩字形的字体，
# 否则会画成一排方框。两类都找不到时分别退回位图字体和英文提示。
FONT_CANDIDATES = (
    '/System/Library/Fonts/Supplemental/Arial.ttf',
    '/Library/Fonts/Arial.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/usr/share/fonts/TTF/DejaVuSans.ttf',
    '/usr/share/fonts/dejavu/DejaVuSans.ttf',
    '/usr/local/share/fonts/DejaVuSans.ttf',
    '/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf',
)

CJK_FONT_CANDIDATES = (
    '/System/Library/Fonts/PingFang.ttc',
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    '/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc',
)

HINT_TEXT_CJK = '手机扫码打开原视频'
HINT_TEXT_LATIN = 'Scan to open the original video'


class TailSkipped(Exception):
    """片尾无法生成，保留原文件继续后续流程。"""


def tail_settings(config):
    settings = config.get('VIDEO_QR_TAIL')
    return settings if isinstance(settings, dict) else {}


def is_enabled(config):
    return bool(tail_settings(config).get('ENABLED'))


def tail_duration(config):
    duration = tail_settings(config).get('DURATION_SECONDS', 5)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        return 5.0
    return float(duration) if 0 < duration <= 60 else 5.0


def normalize_source_url(value):
    """只接受 http(s) 链接，避免把奇怪的文本画进视频或塞进二维码。"""
    if not isinstance(value, str):
        return ''
    candidate = value.strip()
    if not candidate:
        return ''
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ''
    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
        return ''
    return candidate


def source_url_from_tags(tags):
    """回退方案：从 purl / comment 标签里找原始链接，与 app.py 的取法一致。"""
    if not isinstance(tags, dict):
        return ''
    normalized = {
        str(key).lower(): value
        for key, value in tags.items()
        if isinstance(value, str)
    }
    for tag_name in ('purl', 'comment'):
        for raw_url in re.findall(
            r'https?://[^\s一-龥　-〿＀-￯]+',
            normalized.get(tag_name, ''),
            flags=re.IGNORECASE,
        ):
            source_url = normalize_source_url(
                raw_url.rstrip('.,;:)]\'"。，；：）、）')
            )
            if source_url:
                return source_url
    return ''


def run_ffmpeg(command, logger, stdin=None):
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            stdin=stdin,
        )
    except FileNotFoundError as exc:
        raise TailSkipped(f'找不到 {command[0]}') from exc
    except subprocess.TimeoutExpired as exc:
        raise TailSkipped(f'{command[0]} 执行超时') from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or '').strip().splitlines()
        raise TailSkipped(
            f'{command[0]} 失败: {stderr[-1] if stderr else exc}'
        ) from exc


def probe_media(video_path, logger):
    result = run_ffmpeg(
        [
            'ffprobe', '-v', 'error',
            '-show_entries',
            'format=duration:format_tags=purl,comment'
            ':stream=index,codec_type,codec_name,width,height,r_frame_rate'
            ',pix_fmt,profile,sample_rate,channels,time_base',
            '-of', 'json', video_path,
        ],
        logger,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise TailSkipped('ffprobe 输出无法解析') from exc
    if not isinstance(payload, dict):
        raise TailSkipped('ffprobe 输出无法解析')
    return payload


def timescale_from_time_base(time_base):
    """mp4 的 time_base 形如 1/90000，片尾要用同一个时间基准。"""
    if not isinstance(time_base, str) or '/' not in time_base:
        return None
    numerator, _, denominator = time_base.partition('/')
    if numerator.strip() != '1':
        return None
    try:
        timescale = int(denominator)
    except ValueError:
        return None
    return timescale if timescale > 0 else None


def describe_layout(payload):
    """检查流布局能否无损拼接，返回生成片尾所需的参数。"""
    streams = payload.get('streams')
    if not isinstance(streams, list) or not streams:
        raise TailSkipped('没有读到媒体流')

    videos, audios, subtitles = [], [], []
    for stream in streams:
        if not isinstance(stream, dict):
            raise TailSkipped('流信息异常')
        codec_type = stream.get('codec_type')
        if codec_type == 'video':
            videos.append(stream)
        elif codec_type == 'audio':
            audios.append(stream)
        elif codec_type == 'subtitle':
            subtitles.append(stream)
        else:
            # bin_data 等数据流没法在片尾造出对应轨道，拼接会丢流
            raise TailSkipped(f'存在无法复制的 {codec_type} 流')

    if len(videos) != 1:
        raise TailSkipped(f'视频流数量为 {len(videos)}')
    if len(audios) > 1:
        raise TailSkipped(f'音频流数量为 {len(audios)}')

    video = videos[0]
    codec_name = video.get('codec_name')
    if codec_name not in SUPPORTED_VIDEO_CODECS:
        raise TailSkipped(f'视频编码 {codec_name} 无法无损拼接')
    pix_fmt = video.get('pix_fmt')
    if pix_fmt not in SUPPORTED_PIXEL_FORMATS:
        raise TailSkipped(f'像素格式 {pix_fmt} 暂不支持')
    width, height = video.get('width'), video.get('height')
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise TailSkipped('没有读到分辨率')
    frame_rate = video.get('r_frame_rate')
    if not isinstance(frame_rate, str) or frame_rate in {'', '0/0'}:
        raise TailSkipped('没有读到帧率')

    for subtitle in subtitles:
        if subtitle.get('codec_name') not in SUPPORTED_SUBTITLE_CODECS:
            raise TailSkipped(f"字幕编码 {subtitle.get('codec_name')} 暂不支持")

    audio = audios[0] if audios else None
    audio_encoder = None
    if audio is not None:
        audio_encoder = AUDIO_ENCODERS.get(audio.get('codec_name'))
        if not audio_encoder:
            raise TailSkipped(f"音频编码 {audio.get('codec_name')} 暂不支持")
        # HE-AAC 这类带 SBR 的音频，容器时间基是采样率的一半；片尾按采样率编码
        # 会被按一半的时间基解读，拼接后整段时长翻倍，只能跳过。
        audio_timescale = timescale_from_time_base(audio.get('time_base'))
        try:
            sample_rate = int(audio.get('sample_rate'))
        except (TypeError, ValueError):
            raise TailSkipped('没有读到音频采样率')
        if audio_timescale != sample_rate:
            raise TailSkipped(
                f"音频时间基 {audio.get('time_base')} 与采样率 {sample_rate} 不一致"
            )

    profile = str(video.get('profile') or '').strip().lower()
    return {
        'width': width,
        'height': height,
        'frame_rate': frame_rate,
        'pix_fmt': 'yuv420p',
        'profile': H264_PROFILES.get(profile, 'high'),
        'timescale': timescale_from_time_base(video.get('time_base')),
        'audio_encoder': audio_encoder,
        'sample_rate': str(audio.get('sample_rate') or '48000') if audio else '',
        'channels': int(audio.get('channels') or 2) if audio else 0,
        'subtitle_count': len(subtitles),
    }


def load_font(paths, size):
    from PIL import ImageFont

    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return None


def resolve_font(config, size, logger):
    """画链接用的拉丁字体，优先使用配置里指定的字体。"""
    from PIL import ImageFont

    candidates = []
    configured = tail_settings(config).get('FONT_PATH')
    if isinstance(configured, str) and configured.strip():
        candidates.append(configured.strip())
    candidates.extend(FONT_CANDIDATES)
    font = load_font(candidates, size)
    if font is not None:
        return font
    logger.info('片尾没有找到可用字体，改用 Pillow 内置位图字体')
    return ImageFont.load_default()


def resolve_hint(config, size, logger):
    """返回 (提示语, 字体)：没有中日韩字体时改用英文，避免画成方框。"""
    configured = tail_settings(config).get('FONT_PATH')
    candidates = []
    if isinstance(configured, str) and configured.strip():
        candidates.append(configured.strip())
    candidates.extend(CJK_FONT_CANDIDATES)
    font = load_font(candidates, size)
    if font is not None:
        return HINT_TEXT_CJK, font
    logger.info('片尾没有找到中文字体，提示语改用英文')
    return HINT_TEXT_LATIN, resolve_font(config, size, logger)


def render_tail_image(source_url, layout, config, output_path, logger):
    """深色背景 + 居中二维码 + 下方原链接文字，尺寸与正片一致。"""
    import segno
    from PIL import Image, ImageDraw

    settings = tail_settings(config)
    background = str(settings.get('BACKGROUND') or '#101820')
    text_color = str(settings.get('TEXT_COLOR') or '#FFFFFF')
    width, height = layout['width'], layout['height']

    canvas = Image.new('RGB', (width, height), background)
    qr_side = max(120, int(min(width, height) * 0.45))
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as qr_file:
        qr_path = qr_file.name
    try:
        # error='m' 在遮挡或反光时也还能扫出来，URL 不长，容量足够
        segno.make(source_url, error='m').save(qr_path, scale=10, border=2)
        with Image.open(qr_path) as qr_image:
            qr = qr_image.convert('RGB').resize((qr_side, qr_side), Image.NEAREST)
    finally:
        try:
            os.remove(qr_path)
        except OSError:
            pass

    # 二维码留白底色必须是白的，扫码器才认；所以给它加一圈白边再贴上去
    padding = max(8, qr_side // 20)
    plate = Image.new('RGB', (qr_side + padding * 2, qr_side + padding * 2), '#FFFFFF')
    plate.paste(qr, (padding, padding))
    plate_x = (width - plate.width) // 2
    plate_y = max(int(height * 0.12), (height - plate.height) // 2 - int(height * 0.08))
    canvas.paste(plate, (plate_x, plate_y))

    draw = ImageDraw.Draw(canvas)
    font = resolve_font(config, max(18, height // 28), logger)
    hint, hint_font = resolve_hint(config, max(20, height // 26), logger)

    def centered(text, drawing_font, top):
        left, upper, right, lower = draw.textbbox((0, 0), text, font=drawing_font)
        draw.text(
            ((width - (right - left)) // 2 - left, top - upper),
            text,
            fill=text_color,
            font=drawing_font,
        )
        return lower - upper

    text_top = plate_y + plate.height + max(16, height // 36)
    text_top += centered(hint, hint_font, text_top) + max(10, height // 60)
    # 链接过长时截断，二维码才是主要入口，文字只是给人看的提示
    display_url = source_url if len(source_url) <= 72 else source_url[:69] + '...'
    centered(display_url, font, text_top)

    canvas.save(output_path)


def build_tail_clip(image_path, layout, duration, output_path, logger):
    command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
               '-loop', '1', '-framerate', layout['frame_rate'], '-i', image_path]
    if layout['audio_encoder']:
        command += [
            '-f', 'lavfi',
            '-i', f"anullsrc=r={layout['sample_rate']}:cl="
                  f"{'mono' if layout['channels'] == 1 else 'stereo'}",
        ]
    # 空字幕轨：正片有几条 mov_text，片尾就补几条，否则 concat 会因流不匹配失败
    for _ in range(layout['subtitle_count']):
        command += ['-f', 'srt', '-i', os.devnull]

    command += ['-t', f'{duration:g}', '-map', '0:v:0']
    next_input = 1
    if layout['audio_encoder']:
        command += ['-map', f'{next_input}:a:0']
        next_input += 1
    for _ in range(layout['subtitle_count']):
        command += ['-map', f'{next_input}:s:0']
        next_input += 1

    command += [
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
        '-profile:v', layout['profile'], '-pix_fmt', layout['pix_fmt'],
        '-r', layout['frame_rate'], '-s', f"{layout['width']}x{layout['height']}",
        # 片尾短，全部关键帧更利于拼接点的解码
        '-g', '25', '-sc_threshold', '0',
    ]
    if layout['audio_encoder']:
        command += [
            '-c:a', layout['audio_encoder'], '-b:a', '128k',
            '-ar', layout['sample_rate'], '-ac', str(layout['channels']),
        ]
    if layout['subtitle_count']:
        command += ['-c:s', 'mov_text']
    if layout['timescale']:
        command += ['-video_track_timescale', str(layout['timescale'])]
    command += ['-movflags', '+faststart', output_path]
    run_ffmpeg(command, logger, stdin=subprocess.DEVNULL)


def concat_clips(video_path, tail_path, list_path, output_path, logger):
    with open(list_path, 'w', encoding='utf-8') as handle:
        for path in (video_path, tail_path):
            # concat demuxer 的转义规则：单引号要拆开写
            escaped = os.path.abspath(path).replace("'", "'\\''")
            handle.write(f"file '{escaped}'\n")
    run_ffmpeg(
        [
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'concat', '-safe', '0', '-i', list_path,
            '-c', 'copy', '-map', '0',
            '-movflags', '+faststart', output_path,
        ],
        logger,
        stdin=subprocess.DEVNULL,
    )


def stream_counts(payload):
    counts = {}
    for stream in payload.get('streams') or []:
        if isinstance(stream, dict):
            codec_type = stream.get('codec_type')
            counts[codec_type] = counts.get(codec_type, 0) + 1
    return counts


def media_duration(payload):
    try:
        return float((payload.get('format') or {}).get('duration'))
    except (TypeError, ValueError):
        return None


def verify_output(output_path, source_payload, duration, logger):
    """拼接后长度和流布局都要对得上，否则宁可保留原文件。"""
    payload = probe_media(output_path, logger)
    if stream_counts(payload) != stream_counts(source_payload):
        raise TailSkipped('拼接后的流布局与原文件不一致')
    source_duration = media_duration(source_payload)
    output_duration = media_duration(payload)
    if source_duration is None or output_duration is None:
        raise TailSkipped('拼接后无法读取时长')
    if abs(output_duration - (source_duration + duration)) > 1.0:
        raise TailSkipped(
            f'拼接后时长异常: {output_duration:.1f}s，预期 {source_duration + duration:.1f}s'
        )


def append_qr_tail(video_path, source_url, config, logger):
    """给视频追加二维码片尾，成功返回 True；不支持或失败时保留原文件返回 False。"""
    if not is_enabled(config):
        return False
    if os.path.splitext(video_path)[1].lower() != '.mp4':
        return False
    if not os.path.isfile(video_path):
        logger.warning('片尾跳过，文件不存在: %s', video_path)
        return False

    basename = os.path.basename(video_path)
    work_dir = os.path.dirname(os.path.abspath(video_path))
    temporary_paths = []
    try:
        payload = probe_media(video_path, logger)
        url = normalize_source_url(source_url) or source_url_from_tags(
            (payload.get('format') or {}).get('tags')
        )
        if not url:
            raise TailSkipped('没有可用的原始链接')
        layout = describe_layout(payload)
        duration = tail_duration(config)

        prefix = f'.qr-tail-{os.getpid()}-'
        image_path = os.path.join(work_dir, f'{prefix}{basename}.png')
        tail_path = os.path.join(work_dir, f'{prefix}{basename}.tail.mp4')
        list_path = os.path.join(work_dir, f'{prefix}{basename}.txt')
        output_path = os.path.join(work_dir, f'{prefix}{basename}')
        temporary_paths = [image_path, tail_path, list_path, output_path]

        render_tail_image(url, layout, config, image_path, logger)
        build_tail_clip(image_path, layout, duration, tail_path, logger)
        concat_clips(video_path, tail_path, list_path, output_path, logger)
        verify_output(output_path, payload, duration, logger)
        os.replace(output_path, video_path)
        temporary_paths.remove(output_path)
        logger.info('已追加 %.0f 秒二维码片尾: %s', duration, basename)
        return True
    except TailSkipped as exc:
        logger.info('跳过二维码片尾 (%s): %s', exc, basename)
        return False
    except Exception as exc:  # 片尾是锦上添花，任何意外都不该影响下载结果
        logger.warning('追加二维码片尾失败 (%s): %s', exc, basename)
        return False
    finally:
        for path in temporary_paths:
            try:
                os.remove(path)
            except OSError:
                pass
