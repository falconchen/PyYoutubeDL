#!/usr/bin/env python
import os
import errno
import sys
import time
import shutil
import subprocess
import json
import logging
import tempfile
from logging.handlers import RotatingFileHandler
from concurrent.futures import ThreadPoolExecutor
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from datetime import datetime
from bark_util import bark_notify
from config_util import MOVE_STAGING_PREFIX, build_dated_output_template, load_config
from log_util import setup_logger

# 加载配置
config = load_config()

# 创建必要的目录
for folder in [config["LOG_DIR"], config["URLS_DIR"], config["TMP_DIR"], config["FILES_DIR"]]:
    os.makedirs(folder, exist_ok=True)

# 配置日志
logger = setup_logger(
    name='downloader',
    log_dir=config["LOG_DIR"],
    log_file='downloader.log',
    max_bytes=config["MAX_LOG_SIZE"],
    backup_count=config["BACKUP_COUNT"],
    timezone=config.get("TIMEZONE", "UTC")
)

VIDEO_OUTPUT_EXTENSIONS = {'.avi', '.flv', '.mkv', '.mov', '.mp4', '.webm'}
AUDIO_OUTPUT_EXTENSIONS = {'.aac', '.flac', '.m4a', '.mp3', '.ogg', '.opus', '.wav'}
SUBTITLE_LANGUAGE_PREFERENCES = ('zh-Hans', 'zh-Hant', 'zh', 'en')
SUBTITLE_TRANSLATION_PREFIXES = ('zh-Hans-', 'zh-Hant-', 'zh-', 'en-')
NON_SUMMARY_SUBTITLE_LANGUAGES = {
    'live_chat',
    'danmaku',
}
SUBTITLE_PROBE_TIMEOUT_SECONDS = 120


def _available_subtitle_languages(subtitle_map):
    """返回确实包含格式且适合 AI 总结的字幕语言代码。"""
    if not isinstance(subtitle_map, dict):
        return []
    return [
        language
        for language, formats in subtitle_map.items()
        if (
            isinstance(language, str)
            and language
            and language.lower() not in NON_SUMMARY_SUBTITLE_LANGUAGES
            and isinstance(formats, list)
            and formats
        )
    ]


def _find_subtitle_language(languages, candidates):
    """按候选顺序查找语言代码，同时兼容大小写差异。"""
    language_by_lowercase = {
        language.lower(): language for language in languages
    }
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        matched = language_by_lowercase.get(candidate.lower())
        if matched:
            return matched
    return None


def select_subtitle_fallback(video_info):
    """配置未匹配字幕时，为 AI 总结选择一条准确性优先的回退字幕。"""
    if not isinstance(video_info, dict):
        return None
    if video_info.get('requested_subtitles'):
        return None

    manual_languages = _available_subtitle_languages(
        video_info.get('subtitles')
    )
    automatic_captions = video_info.get('automatic_captions')
    automatic_languages = _available_subtitle_languages(automatic_captions)
    source_language = video_info.get('language')
    source_candidates = [source_language]
    if isinstance(source_language, str) and '-' in source_language:
        source_candidates.append(source_language.split('-', 1)[0])

    # 人工原文字幕的准确性通常高于自动翻译，因此优先使用任意人工字幕。
    selected = _find_subtitle_language(
        manual_languages,
        SUBTITLE_LANGUAGE_PREFERENCES,
    )
    if not selected:
        selected = _find_subtitle_language(manual_languages, source_candidates)
    if not selected and manual_languages:
        selected = manual_languages[0]
    if selected:
        return selected, '人工字幕'

    selected = _find_subtitle_language(
        automatic_languages,
        SUBTITLE_LANGUAGE_PREFERENCES,
    )
    if not selected:
        selected = _find_subtitle_language(
            automatic_languages,
            source_candidates,
        )

    # YouTube 的原文自动字幕通常没有 “from ...” 后缀；用它补足来源
    # 语言元数据缺失或语言变体不一致的情况。
    if not selected and isinstance(automatic_captions, dict):
        for language in automatic_languages:
            formats = automatic_captions.get(language) or []
            names = [
                item.get('name')
                for item in formats
                if isinstance(item, dict) and isinstance(item.get('name'), str)
            ]
            if names and all(' from ' not in name.lower() for name in names):
                selected = language
                break

    if selected:
        return selected, '自动原文字幕'

    for prefix in SUBTITLE_TRANSLATION_PREFIXES:
        selected = next(
            (
                language for language in automatic_languages
                if language.lower().startswith(prefix.lower())
            ),
            None,
        )
        if selected:
            return selected, '自动翻译字幕'

    if automatic_languages:
        return automatic_languages[0], '自动字幕'
    return None


def _first_video_info(info):
    """从单视频或播放列表元数据中取出首个有效视频条目。"""
    if not isinstance(info, dict):
        return None
    entries = info.get('entries')
    if isinstance(entries, list):
        for entry in entries:
            video_info = _first_video_info(entry)
            if video_info:
                return video_info
        return None
    return info


def probe_subtitle_fallback(url, conf_path):
    """使用实际 yt-dlp 配置预检字幕，仅在配置未匹配时返回回退项。"""
    cmd = [
        'yt-dlp',
        '--config-location', conf_path,
        '--simulate',
        '--skip-download',
        '--playlist-end', '1',
        '--sleep-requests', '0',
        '--sleep-interval', '0',
        '--max-sleep-interval', '0',
        '--sleep-subtitles', '0',
        '--dump-single-json',
        url,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SUBTITLE_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("字幕预检执行失败，继续使用原配置: %s", exc)
        return None

    if result.returncode != 0:
        logger.warning(
            "字幕预检返回失败状态 %s，继续使用原配置",
            result.returncode,
        )
        return None
    try:
        video_info = _first_video_info(json.loads(result.stdout))
    except (TypeError, json.JSONDecodeError) as exc:
        logger.warning("字幕预检结果无法解析，继续使用原配置: %s", exc)
        return None

    fallback = select_subtitle_fallback(video_info)
    if fallback:
        language, subtitle_type = fallback
        logger.info(
            "当前配置未匹配字幕，动态回退到%s: %s",
            subtitle_type,
            language,
        )
    elif video_info and not video_info.get('requested_subtitles'):
        logger.info("字幕预检未发现可用字幕")
    return fallback


def destination_with_counter(destination, counter):
    """为同名文件生成 `文件名 (N).扩展名` 形式的候选路径。"""
    if counter == 0:
        return destination
    stem, extension = os.path.splitext(destination)
    return f"{stem} ({counter}){extension}"


def link_to_unique_destination(source, destination):
    """使用硬链接原子发布文件，目标存在时自动递增文件名。"""
    counter = 0
    while True:
        candidate = destination_with_counter(destination, counter)
        try:
            os.link(source, candidate)
            return candidate
        except FileExistsError:
            counter += 1


def move_without_overwrite(source, destination):
    """移动文件且绝不覆盖目标；跨文件系统时先在目标目录暂存。"""
    try:
        final_destination = link_to_unique_destination(source, destination)
    except OSError as exc:
        unsupported_link_errors = {
            errno.EXDEV,
            errno.EPERM,
            getattr(errno, 'ENOTSUP', errno.EOPNOTSUPP),
            errno.EOPNOTSUPP,
        }
        if exc.errno not in unsupported_link_errors:
            raise

        destination_dir = os.path.dirname(destination)
        fd, staging_path = tempfile.mkstemp(
            prefix=MOVE_STAGING_PREFIX,
            dir=destination_dir,
        )
        os.close(fd)
        try:
            shutil.copy2(source, staging_path)
            final_destination = link_to_unique_destination(
                staging_path,
                destination,
            )
        finally:
            if os.path.exists(staging_path):
                os.remove(staging_path)

    os.remove(source)
    return final_destination


def select_primary_media_file(filepaths, mode, file_sizes=None):
    """从最终产物中选择最大的主媒体文件，字幕等辅助文件不参与。"""
    extensions = (
        AUDIO_OUTPUT_EXTENSIONS if mode == 'audio'
        else VIDEO_OUTPUT_EXTENSIONS
    )
    candidates = []
    for filepath in filepaths:
        extension = os.path.splitext(filepath)[1].lower()
        if extension not in extensions:
            continue
        if file_sizes is not None and filepath in file_sizes:
            size = file_sizes[filepath]
        elif os.path.isfile(filepath):
            size = os.path.getsize(filepath)
        else:
            continue
        candidates.append((size, os.path.basename(filepath), filepath))
    if not candidates:
        return None
    return max(candidates)[2]


def build_task_summary(filepaths, mode, elapsed_seconds, file_sizes=None):
    """根据最终主媒体和完整处理耗时生成任务完成摘要。"""
    primary_file = select_primary_media_file(filepaths, mode, file_sizes)
    if primary_file is None:
        return None

    final_size_bytes = (
        file_sizes[primary_file]
        if file_sizes is not None and primary_file in file_sizes
        else os.path.getsize(primary_file)
    )
    elapsed_seconds = max(0.0, float(elapsed_seconds))
    average_speed = (
        final_size_bytes / elapsed_seconds if elapsed_seconds > 0 else 0.0
    )
    return {
        "primary_file": os.path.basename(primary_file),
        "final_size_bytes": final_size_bytes,
        "elapsed_seconds": elapsed_seconds,
        "average_speed_bytes_per_second": average_speed,
    }


def write_task_result(task_id, filenames, summary=None):
    """原子写入任务最终产物清单，供 Web 页面生成精确播放链接。"""
    result_path = os.path.join(config["URLS_DIR"], f"{task_id}.result.json")
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            prefix=f'.{task_id}.',
            suffix='.tmp',
            dir=config["URLS_DIR"],
            delete=False,
        ) as result_file:
            temporary_path = result_file.name
            result_data = {"files": filenames}
            if summary:
                result_data["summary"] = summary
            json.dump(result_data, result_file, ensure_ascii=False)
            result_file.flush()
            os.fsync(result_file.fileno())
        os.replace(temporary_path, result_path)
    except OSError as exc:
        logger.warning("写入任务产物清单失败: %s (%s)", task_id, exc)
        if temporary_path and os.path.exists(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError:
                pass


class DownloadHandler(FileSystemEventHandler):
    def __init__(self, executor):
        super().__init__()
        self.executor = executor

    def on_created(self, event):
        """
        当在监控目录中创建新文件（.txt）时触发。

        Args:
            event: 文件系统事件对象。
        """
        if not event.is_directory and event.src_path.endswith('.txt') and os.path.exists(event.src_path):
            logger.info(f"检测到新文件: {event.src_path}")
            self.executor.submit(self.process_file, event.src_path)

    def on_moved(self, event):
        """
        当在监控目录中文件发生移动或重命名（为 .txt）时触发。

        Args:
            event: 文件系统事件对象。
        """
        if not event.is_directory and event.dest_path.endswith('.txt') and os.path.exists(event.dest_path):
            logger.info(f"检测到文件重命名为txt: {event.src_path} -> {event.dest_path}")
            self.executor.submit(self.process_file, event.dest_path)

    def process_file(self, filepath):
        """
        处理 .txt 任务文件：解析 URL、重命名任务状态、发起下载并根据结果更新状态。

        Args:
            filepath (str): 任务文件的本地路径。
        """
        time.sleep(0.5)
        if not os.path.exists(filepath):
            logger.warning(f"文件已不存在: {filepath}")
            return
        try:
            with open(filepath, 'r') as f:
                url = f.read().strip()
                if not url:
                    logger.warning(f"文件内容为空: {filepath}")
                    return

            base_name = os.path.splitext(os.path.basename(filepath))[0]
            # 下载前先重命名为.downloading
            downloading_path = filepath.rsplit('.', 1)[0] + '.downloading'
            try:
                os.rename(filepath, downloading_path)
                started_at = time.monotonic()
                logger.info(f"任务开始，文件重命名为: {downloading_path}")
            except Exception as e:
                logger.error(f"重命名为.downloading失败: {e}")
                return
            # 根据首字母判断模式
            mode = 'audio' if base_name[0] == 'a' else 'video'
            result = self.download(
                url,
                base_name,
                mode,
                started_at=started_at,
            )
            new_extension = '.ok' if result else '.fail'
            new_filepath = downloading_path.rsplit('.', 1)[0] + new_extension
            os.rename(downloading_path, new_filepath)
            logger.info(f"任务完成，文件重命名为: {new_filepath}")
            # bark_notify(config['BARK_DEVICE_TOKEN'],
            #             title="下载完成" if result else "下载失败",
            #             content=f"{url} 下载{'完成' if result else '失败'}，文件: {os.path.basename(new_filepath)}")
        except Exception as e:
            logger.error(f"处理文件失败: {filepath}, 错误信息: {e}")
            bark_notify(config['BARK_DEVICE_TOKEN'],
                        title="下载失败",
                        content=f"{url} 下载失败，错误信息: {e}")

    def download(self, url, base_name, mode, started_at=None):
        """
        使用 yt-dlp 调用外部命令行执行视频/音频下载。

        Args:
            url (str): 视频/音频的 URL。
            base_name (str): 任务基础名称（用于日志和临时目录）。
            mode (str): 'video' 或 'audio' 模式。
            started_at (float | None): 任务进入 downloading 状态时的单调时钟。

        Returns:
            bool: 下载成功返回 True，失败返回 False。
        """
        logger.info(f"开始下载: {url} ({mode})")
        default_conf_file = 'yt-dlp.conf' if mode == 'video' else 'yta-dlp.conf'
        script_dir = os.path.dirname(os.path.abspath(__file__))
        
        # 检查是否存在.local.conf文件
        local_conf_file = default_conf_file.replace('.conf', '.local.conf')
        local_conf_path = os.path.join(script_dir, local_conf_file)
        default_conf_path = os.path.join(script_dir, default_conf_file)
        
        # 优先使用.local.conf文件，如果不存在则使用默认配置文件
        conf_path = local_conf_path if os.path.exists(local_conf_path) else default_conf_path
        logger.info(f"使用配置文件: {conf_path}")
        
        task_tmp_dir = os.path.join(config["TMP_DIR"], f"{base_name}")
        log_basename = os.path.basename(task_tmp_dir)
        log_path = os.path.join(config["LOG_DIR"], f"{log_basename}.log")
        base_output_template = (
            config["YT_DLP_OUTPUT_TEMPLATE"]
            if mode == 'video'
            else config["YTA_DLP_OUTPUT_TEMPLATE"]
        )
        output_template = build_dated_output_template(
            base_output_template,
            config.get("TIMEZONE", "UTC"),
        )

        dynamic_subtitle_args = []
        if mode == 'video':
            subtitle_fallback = probe_subtitle_fallback(url, conf_path)
            if subtitle_fallback:
                dynamic_subtitle_args = [
                    '--sub-langs',
                    subtitle_fallback[0],
                ]
        
        # 核心修改：添加 --newline 和 --progress 确保进度条被捕获
        cmd = [
            'yt-dlp',
            '--config-location', conf_path,
            '--add-metadata',     # 视频和音频统一在运行时写入媒体元信息
            '--newline',           # 强制进度输出换行，以便逐行读取
            '--progress',          # 强制显示进度条（即使在管道中运行）
            '--progress-template',
            (
                'download:PYDL_PROGRESS|%(progress.status)s|'
                '%(progress._percent_str)s|%(progress._downloaded_bytes_str)s|'
                '%(progress._total_bytes_str)s|%(progress._speed_str)s|'
                '%(progress._eta_str)s|%(info.ext)s|%(info.format_id)s|'
                '%(info.vcodec)s|%(info.acodec)s'
            ),
            *dynamic_subtitle_args,
            '-o', os.path.join(task_tmp_dir, output_template),
            url
        ]

        try:
            os.makedirs(task_tmp_dir, exist_ok=True)
        except Exception as e:
            logger.error(f"创建临时目录失败: {e}")
            return False

        try:
            # buffering=1 开启行级缓存
            with open(log_path, 'w', encoding='utf-8', buffering=1) as log_file:
                process = subprocess.Popen(
                    cmd, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.STDOUT, 
                    universal_newlines=True,
                    bufsize=1  # 对应 Popen 的行缓冲
                )
                
                # 实时循环读取
                for line in process.stdout:
                    stripped = line.rstrip('\n')
                    # 1. 实时写入任务专属日志文件
                    log_file.write(line)
                    # 2. 强制刷新，确保在 log 文件里能即时看到内容
                    log_file.flush()
                    # 3. 同时写入 logger（downloader.log），级别使用 info
                    logger.info(stripped)
                
                process.wait()
                if process.returncode != 0:
                    raise subprocess.CalledProcessError(process.returncode, cmd)
            
            if not self.move_files(
                task_tmp_dir,
                task_id=base_name,
                mode=mode,
                started_at=started_at,
            ):
                logger.error(f"下载产物移动失败，临时文件已保留: {task_tmp_dir}")
                return False
            logger.info(f"下载完成: {url}")
            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"下载失败: {url}，错误信息: {e}")
            # 下载失败时删除临时目录
            if os.path.exists(task_tmp_dir):
                try:
                    import shutil
                    shutil.rmtree(task_tmp_dir)
                    logger.info(f"下载失败，已删除临时目录: {task_tmp_dir}")
                except Exception as del_e:
                    logger.error(f"下载失败，删除临时目录失败: {task_tmp_dir}, 错误信息: {del_e}")
            
            bark_notify(config['BARK_DEVICE_TOKEN'],
                        title="下载失败",
                        content=f"{url} 下载失败，错误信息: {e}")
            return False

    def move_files(self, tmp_dir, task_id=None, mode=None, started_at=None):
        """
        将下载完成的文件从临时目录移动到正式的文件输出目录。

        Args:
            tmp_dir (str): 下载任务的临时目录路径。
            task_id (str | None): 任务 ID；提供时记录最终产物文件名。
            mode (str | None): video 或 audio，用于选择最终主媒体。
            started_at (float | None): 完整处理计时起点。
        """
        move_succeeded = True
        moved_filenames = []
        moved_filepaths = []
        moved_file_sizes = {}
        for filename in os.listdir(tmp_dir):
            src = os.path.join(tmp_dir, filename)
            dst = os.path.join(config["FILES_DIR"], filename)
            if not os.path.exists(src):
                logger.warning(f"源文件不存在，跳过处理: {src}")
                continue
            try:
                source_size = os.path.getsize(src)
                final_dst = move_without_overwrite(src, dst)
                if final_dst != dst:
                    logger.info(f"目标文件已存在，自动重命名为: {os.path.basename(final_dst)}")
                logger.info(f"已移动文件: {src} -> {final_dst}")
                moved_filenames.append(os.path.basename(final_dst))
                moved_filepaths.append(final_dst)
                moved_file_sizes[final_dst] = source_size
            except Exception as e:
                logger.error(f"移动文件失败: {src}, 错误信息: {e}")
                move_succeeded = False
        if move_succeeded and os.path.exists(tmp_dir):
            try:
                shutil.rmtree(tmp_dir)
                logger.info(f"已删除临时目录: {tmp_dir}")
            except Exception as e:
                logger.error(f"删除临时目录失败: {tmp_dir}, 错误信息: {e}")
                move_succeeded = False
        if move_succeeded and task_id:
            summary = None
            if mode in {'video', 'audio'} and started_at is not None:
                summary = build_task_summary(
                    moved_filepaths,
                    mode,
                    time.monotonic() - started_at,
                    moved_file_sizes,
                )
            write_task_result(task_id, moved_filenames, summary=summary)
        return move_succeeded

def start_monitor(folder):
    """
    启动文件系统监控器和线程池。

    Args:
        folder (str): 要监控的目录路径。

    Returns:
        Observer: 已启动的 watchdog 观察者对象。
    """
    executor = ThreadPoolExecutor(max_workers=config["MAX_WORKERS"])
    event_handler = DownloadHandler(executor)
    observer = Observer()
    observer.schedule(event_handler, folder, recursive=False)
    observer.start()
    logger.info(f"开始监控目录: {folder}")
    return observer

def main():
    """
    程序主入口，监控 URLS_DIR 目录。
    """
    observer = start_monitor(config["URLS_DIR"])
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == '__main__':
    main()
