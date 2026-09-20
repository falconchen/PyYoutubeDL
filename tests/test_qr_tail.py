"""视频片尾二维码：参数拼装、跳过分支，以及一次真实的端到端拼接。"""
import json
import logging
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import qr_tail

LOGGER = logging.getLogger('qr_tail_test')
LOGGER.addHandler(logging.NullHandler())

ENABLED_CONFIG = {'VIDEO_QR_TAIL': {'ENABLED': True, 'DURATION_SECONDS': 5}}


def probe_payload(
    video_codec='h264',
    audio_codec='aac',
    subtitle_count=0,
    extra_streams=(),
    duration='60.0',
    tags=None,
    pix_fmt='yuv420p',
    codec_tag='avc1',
    profile='High',
):
    streams = [{
        'index': 0, 'codec_type': 'video', 'codec_name': video_codec,
        'codec_tag_string': codec_tag,
        'width': 1920, 'height': 1080, 'r_frame_rate': '30/1',
        'pix_fmt': pix_fmt, 'profile': profile, 'time_base': '1/15360',
    }]
    if audio_codec:
        streams.append({
            'index': 1, 'codec_type': 'audio', 'codec_name': audio_codec,
            'sample_rate': '48000', 'channels': 2, 'time_base': '1/48000',
        })
    for offset in range(subtitle_count):
        streams.append({
            'index': 2 + offset, 'codec_type': 'subtitle',
            'codec_name': 'mov_text', 'time_base': '1/1000',
        })
    streams.extend(extra_streams)
    return {
        'streams': streams,
        'format': {'duration': duration, 'tags': tags or {}},
    }


class TestLayoutChecks(unittest.TestCase):
    def test_supported_layout_reports_encoder_settings(self):
        layout = qr_tail.describe_layout(probe_payload(subtitle_count=3))

        self.assertEqual(layout['width'], 1920)
        self.assertEqual(layout['frame_rate'], '30/1')
        self.assertEqual(layout['profile'], 'high')
        self.assertEqual(layout['timescale'], 15360)
        self.assertEqual(layout['audio_encoder'], 'aac')
        self.assertEqual(layout['subtitle_count'], 3)

    def test_opus_audio_uses_libopus(self):
        layout = qr_tail.describe_layout(probe_payload(audio_codec='opus'))
        self.assertEqual(layout['audio_encoder'], 'libopus')

    def test_video_only_file_needs_no_audio_encoder(self):
        layout = qr_tail.describe_layout(probe_payload(audio_codec=None))
        self.assertIsNone(layout['audio_encoder'])

    def test_hevc_and_av1_pick_their_own_encoder_and_keep_the_tag(self):
        hevc = qr_tail.describe_layout(probe_payload(
            video_codec='hevc', codec_tag='hev1', profile='Main',
        ))
        self.assertEqual(hevc['video_encoder'], 'libx265')
        self.assertEqual(hevc['profile'], 'main')
        # tag 跟随片源，写成 hvc1 会让部分设备认不出来
        self.assertEqual(hevc['codec_tag'], 'hev1')

        av1 = qr_tail.describe_layout(probe_payload(
            video_codec='av1', codec_tag='av01', profile='Main',
        ))
        self.assertEqual(av1['video_encoder'], 'libsvtav1')
        self.assertEqual(av1['codec_tag'], 'av01')

    def test_ten_bit_sources_keep_their_pixel_format_and_profile(self):
        hevc = qr_tail.describe_layout(probe_payload(
            video_codec='hevc', pix_fmt='yuv420p10le', codec_tag='hvc1',
        ))
        self.assertEqual(hevc['pix_fmt'], 'yuv420p10le')
        self.assertEqual(hevc['profile'], 'main10')

        h264 = qr_tail.describe_layout(probe_payload(pix_fmt='yuv420p10le'))
        self.assertEqual(h264['profile'], 'high10')

    def test_unsupported_codec_is_skipped(self):
        with self.assertRaises(qr_tail.TailSkipped):
            qr_tail.describe_layout(probe_payload(video_codec='vp9'))

    def test_unsupported_pixel_format_is_skipped(self):
        with self.assertRaises(qr_tail.TailSkipped):
            qr_tail.describe_layout(probe_payload(pix_fmt='yuv444p'))

    def test_data_stream_is_skipped(self):
        payload = probe_payload(
            extra_streams=({'index': 3, 'codec_type': 'data', 'codec_name': 'bin_data'},),
        )
        with self.assertRaises(qr_tail.TailSkipped):
            qr_tail.describe_layout(payload)

    def test_audio_timebase_mismatch_is_skipped(self):
        """HE-AAC 的容器时间基是采样率的一半，拼接后时长会翻倍。"""
        payload = probe_payload()
        payload['streams'][1]['time_base'] = '1/24000'
        with self.assertRaises(qr_tail.TailSkipped):
            qr_tail.describe_layout(payload)


class TestTailCommand(unittest.TestCase):
    def build_command(self, **layout_overrides):
        layout = qr_tail.describe_layout(probe_payload(**layout_overrides))
        with patch('qr_tail.run_ffmpeg') as run:
            qr_tail.build_tail_clip('/tmp/tail.png', layout, 5, '/tmp/tail.mp4', LOGGER)
        return run.call_args.args[0]

    def test_tail_matches_source_encoding(self):
        command = self.build_command()

        self.assertIn('libx264', command)
        self.assertEqual(command[command.index('-profile:v') + 1], 'high')
        self.assertEqual(command[command.index('-r') + 1], '30/1')
        self.assertEqual(command[command.index('-s') + 1], '1920x1080')
        self.assertEqual(command[command.index('-c:a') + 1], 'aac')
        self.assertEqual(command[command.index('-ar') + 1], '48000')
        self.assertEqual(command[command.index('-video_track_timescale') + 1], '15360')

    def test_tail_uses_the_source_encoder_tag_and_inline_headers(self):
        h264 = self.build_command()
        self.assertEqual(h264[h264.index('-c:v') + 1], 'libx264')
        self.assertEqual(h264[h264.index('-tag:v') + 1], 'avc1')
        # 拼接后轨道只留正片的编码配置，片尾要把参数集写进每个关键帧
        self.assertEqual(h264[h264.index('-x264-params') + 1], 'repeat-headers=1')

        hevc = self.build_command(video_codec='hevc', codec_tag='hvc1', profile='Main')
        self.assertEqual(hevc[hevc.index('-c:v') + 1], 'libx265')
        self.assertEqual(hevc[hevc.index('-tag:v') + 1], 'hvc1')
        self.assertEqual(hevc[hevc.index('-x265-params') + 1], 'repeat-headers=1')

        av1 = self.build_command(video_codec='av1', codec_tag='av01', profile='Main')
        self.assertEqual(av1[av1.index('-c:v') + 1], 'libsvtav1')
        self.assertEqual(av1[av1.index('-tag:v') + 1], 'av01')
        # SVT-AV1 没有内嵌参数集的开关，靠拼接后的解码校验兜底
        self.assertNotIn('-x265-params', av1)
        self.assertNotIn('-x264-params', av1)

    def test_ten_bit_tail_keeps_the_pixel_format(self):
        command = self.build_command(
            video_codec='hevc', pix_fmt='yuv420p10le', codec_tag='hvc1', profile='Main',
        )
        self.assertEqual(command[command.index('-pix_fmt') + 1], 'yuv420p10le')
        self.assertEqual(command[command.index('-profile:v') + 1], 'main10')

    def test_tail_adds_one_empty_subtitle_per_source_track(self):
        command = self.build_command(subtitle_count=2)

        self.assertEqual(command.count(os.devnull), 2)
        self.assertIn('mov_text', command)
        self.assertIn('1:a:0', command)
        self.assertIn('2:s:0', command)
        self.assertIn('3:s:0', command)

    def test_video_only_tail_has_no_audio_options(self):
        command = self.build_command(audio_codec=None)

        self.assertNotIn('-c:a', command)
        self.assertNotIn('anullsrc', ' '.join(command))


class TestConcatCommand(unittest.TestCase):
    def test_concat_copies_metadata_from_the_original_file(self):
        """concat 输入不带原片标签，少了 comment/purl 媒体库就没有封面和原始链接。"""
        with TemporaryDirectory() as directory:
            list_path = str(Path(directory, 'list.txt'))
            with patch('qr_tail.run_ffmpeg') as run:
                qr_tail.concat_clips(
                    '/tmp/video.mp4', '/tmp/tail.mp4', list_path, '/tmp/out.mp4', LOGGER,
                )
            command = run.call_args.args[0]

        self.assertEqual(command[command.index('-map_metadata') + 1], '1')
        self.assertEqual(command[command.index('-map_chapters') + 1], '1')
        # 第二路输入就是原片，元数据从它来
        inputs = [command[i + 1] for i, item in enumerate(command) if item == '-i']
        self.assertEqual(inputs, [list_path, '/tmp/video.mp4'])

    def test_dropped_tags_keep_the_original_file(self):
        source = probe_payload(tags={'title': '标题', 'comment': 'https://e.com/v'})
        with TemporaryDirectory() as directory:
            video = Path(directory, 'video.mp4')
            video.write_bytes(b'original')

            def fake_concat(video_path, tail_path, list_path, output_path, logger):
                Path(output_path).write_bytes(b'joined')

            with (
                patch('qr_tail.probe_media', side_effect=[
                    source,
                    probe_payload(duration='65.0', tags={}),  # 标签丢了
                ]),
                patch('qr_tail.render_tail_image'),
                patch('qr_tail.build_tail_clip'),
                patch('qr_tail.concat_clips', side_effect=fake_concat),
            ):
                appended = qr_tail.append_qr_tail(
                    str(video), 'https://e.com/v', ENABLED_CONFIG, LOGGER
                )

            self.assertFalse(appended)
            self.assertEqual(video.read_bytes(), b'original')


class TestAppendQrTail(unittest.TestCase):
    def test_disabled_config_does_nothing(self):
        with patch('qr_tail.probe_media') as probe:
            self.assertFalse(
                qr_tail.append_qr_tail('/tmp/video.mp4', 'https://e.com/v', {}, LOGGER)
            )
        probe.assert_not_called()

    def test_source_url_falls_back_to_media_tags(self):
        tagged = probe_payload(tags={'purl': 'https://www.youtube.com/watch?v=abc'})
        self.assertEqual(
            qr_tail.source_url_from_tags(tagged['format']['tags']),
            'https://www.youtube.com/watch?v=abc',
        )

    def test_missing_source_url_skips_without_touching_file(self):
        with TemporaryDirectory() as directory:
            video = Path(directory, 'video.mp4')
            video.write_bytes(b'original')
            with patch('qr_tail.probe_media', return_value=probe_payload()):
                appended = qr_tail.append_qr_tail(
                    str(video), '', ENABLED_CONFIG, LOGGER
                )

            self.assertFalse(appended)
            self.assertEqual(video.read_bytes(), b'original')

    def test_failed_concat_keeps_original_and_cleans_temp_files(self):
        with TemporaryDirectory() as directory:
            video = Path(directory, 'video.mp4')
            video.write_bytes(b'original')
            with (
                patch('qr_tail.probe_media', return_value=probe_payload()),
                patch('qr_tail.render_tail_image'),
                patch('qr_tail.build_tail_clip'),
                patch(
                    'qr_tail.concat_clips',
                    side_effect=qr_tail.TailSkipped('ffmpeg 失败'),
                ),
            ):
                appended = qr_tail.append_qr_tail(
                    str(video), 'https://e.com/v', ENABLED_CONFIG, LOGGER
                )

            self.assertFalse(appended)
            self.assertEqual(video.read_bytes(), b'original')
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['video.mp4'])

    def test_wrong_output_duration_keeps_original(self):
        source = probe_payload(duration='60.0')
        with TemporaryDirectory() as directory:
            video = Path(directory, 'video.mp4')
            video.write_bytes(b'original')

            def fake_concat(video_path, tail_path, list_path, output_path, logger):
                Path(output_path).write_bytes(b'joined')

            with (
                patch('qr_tail.probe_media', side_effect=[
                    source,
                    probe_payload(duration='120.0'),  # 拼接结果时长不对
                ]),
                patch('qr_tail.render_tail_image'),
                patch('qr_tail.build_tail_clip'),
                patch('qr_tail.concat_clips', side_effect=fake_concat),
            ):
                appended = qr_tail.append_qr_tail(
                    str(video), 'https://e.com/v', ENABLED_CONFIG, LOGGER
                )

            self.assertFalse(appended)
            self.assertEqual(video.read_bytes(), b'original')


class TestTailImage(unittest.TestCase):
    """链接不能画出画面之外：竖屏和超长链接都要收进可用宽度。"""

    def render(self, width, height, url):
        from PIL import Image

        layout = qr_tail.describe_layout(probe_payload())
        layout.update(width=width, height=height)
        with TemporaryDirectory() as directory:
            output = str(Path(directory, 'tail.png'))
            qr_tail.render_tail_image(url, layout, ENABLED_CONFIG, output, LOGGER)
            with Image.open(output) as image:
                self.assertEqual(image.size, (width, height))
                mask = image.convert('L').point(lambda value: 255 if value > 60 else 0)
                return mask.getbbox()

    def assert_inside(self, bbox, width, height):
        left, top, right, bottom = bbox
        self.assertGreater(left, 0)
        self.assertLess(right, width)
        self.assertGreater(top, 0)
        self.assertLess(bottom, height)

    def test_portrait_video_keeps_url_inside_the_frame(self):
        self.assert_inside(
            self.render(608, 1080, 'https://www.youtube.com/shorts/pJWK-ZExAbc'),
            608, 1080,
        )

    def test_long_url_wraps_instead_of_overflowing(self):
        long_url = (
            'https://www.bilibili.com/video/BV1xx411c7mD'
            '?spm_id_from=333.1007.tianma.1-1-1.click&vd_source=abcdef123456'
        )
        self.assert_inside(self.render(1280, 720, long_url), 1280, 720)

    def test_tracking_params_are_stripped_from_the_qr_and_the_text(self):
        """B 站分享链接的跟踪参数会把画面上的文字挤满，二维码里也没必要带。"""
        self.assertEqual(
            qr_tail.normalize_source_url(
                'https://www.bilibili.com/video/BV1pdet6MEBD'
                '?trackid=web_pegasus_0.abc&spm_id_from=333.1007'
            ),
            'https://www.bilibili.com/video/BV1pdet6MEBD',
        )

    def test_display_url_drops_the_scheme(self):
        self.assertEqual(
            qr_tail.strip_url_scheme('https://www.youtube.com/watch?v=abc'),
            'www.youtube.com/watch?v=abc',
        )


@unittest.skipUnless(
    shutil.which('ffmpeg') and shutil.which('ffprobe'), '需要 ffmpeg 与 ffprobe'
)
class TestTailDecodeCheck(unittest.TestCase):
    """拼接点的参数集不一定被认，片尾必须真的解出来才算成功。"""

    def make_clip(self, path, color):
        subprocess.run(
            [
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'lavfi', '-i', f'color=c={color}:size=320x240:rate=25',
                '-t', '2', '-c:v', 'libx264', '-preset', 'ultrafast',
                '-pix_fmt', 'yuv420p', path,
            ],
            check=True, capture_output=True,
        )

    def make_image(self, path, color):
        subprocess.run(
            [
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'lavfi', '-i', f'color=c={color}:size=320x240',
                '-frames:v', '1', path,
            ],
            check=True, capture_output=True,
        )

    def test_matching_frame_passes(self):
        with TemporaryDirectory() as directory:
            clip = str(Path(directory, 'clip.mp4'))
            image = str(Path(directory, 'tail.png'))
            self.make_clip(clip, 'black')
            self.make_image(image, 'black')
            qr_tail.verify_tail_decodes(clip, image, 0.0, 2.0, LOGGER)

    def test_frame_that_does_not_match_the_tail_image_fails(self):
        with TemporaryDirectory() as directory:
            clip = str(Path(directory, 'clip.mp4'))
            image = str(Path(directory, 'tail.png'))
            self.make_clip(clip, 'black')
            self.make_image(image, 'white')
            with self.assertRaises(qr_tail.TailSkipped):
                qr_tail.verify_tail_decodes(clip, image, 0.0, 2.0, LOGGER)


@unittest.skipUnless(
    shutil.which('ffmpeg') and shutil.which('ffprobe'), '需要 ffmpeg 与 ffprobe'
)
class TestAppendQrTailEndToEnd(unittest.TestCase):
    ENCODERS = {'h264': 'libx264', 'hevc': 'libx265', 'av1': 'libsvtav1'}

    def make_sample(self, directory, extra_args=(), codec='h264'):
        path = str(Path(directory, 'sample.mp4'))
        encoder = self.ENCODERS[codec]
        speed = ['-preset', '10'] if encoder == 'libsvtav1' else ['-preset', 'ultrafast']
        subprocess.run(
            [
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=25',
                '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo',
                '-t', '2', '-c:v', encoder, *speed,
                '-pix_fmt', 'yuv420p', '-c:a', 'aac', *extra_args, path,
            ],
            check=True,
            capture_output=True,
        )
        return path

    def tags(self, path):
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error', '-show_entries', 'format_tags',
                '-of', 'json', path,
            ],
            check=True, capture_output=True, text=True,
        )
        tags = json.loads(result.stdout)['format'].get('tags', {})
        return {
            key: value for key, value in tags.items()
            if key.lower() in {'title', 'artist', 'comment'}
        }

    def describe(self, path):
        result = subprocess.run(
            [
                'ffprobe', '-v', 'error', '-show_entries',
                'format=duration:stream=codec_type', '-of', 'json', path,
            ],
            check=True, capture_output=True, text=True,
        )
        payload = json.loads(result.stdout)
        return (
            float(payload['format']['duration']),
            [stream['codec_type'] for stream in payload['streams']],
        )

    def test_hevc_and_av1_sources_also_get_a_tail(self):
        """B 站常给 HEVC 和 AV1；片尾要按片源编码生成才能无损拼接。"""
        for codec in ('hevc', 'av1'):
            with self.subTest(codec=codec):
                with TemporaryDirectory() as directory:
                    sample = self.make_sample(directory, codec=codec)
                    duration_before, streams_before = self.describe(sample)

                    appended = qr_tail.append_qr_tail(
                        sample,
                        'https://www.bilibili.com/video/BV1xx411c7mD',
                        {'VIDEO_QR_TAIL': {'ENABLED': True, 'DURATION_SECONDS': 2}},
                        LOGGER,
                    )

                    duration_after, streams_after = self.describe(sample)
                    self.assertTrue(appended)
                    self.assertAlmostEqual(
                        duration_after, duration_before + 2, delta=0.5
                    )
                    self.assertEqual(streams_after, streams_before)

    def test_tail_is_appended_without_changing_stream_layout(self):
        with TemporaryDirectory() as directory:
            sample = self.make_sample(directory, extra_args=(
                '-metadata', 'title=样片',
                '-metadata', 'artist=作者',
                '-metadata', 'comment=https://www.youtube.com/watch?v=abc',
            ))
            duration_before, streams_before = self.describe(sample)
            tags_before = self.tags(sample)

            appended = qr_tail.append_qr_tail(
                sample,
                'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
                {'VIDEO_QR_TAIL': {'ENABLED': True, 'DURATION_SECONDS': 2}},
                LOGGER,
            )

            duration_after, streams_after = self.describe(sample)
            self.assertTrue(appended)
            self.assertAlmostEqual(duration_after, duration_before + 2, delta=0.5)
            self.assertEqual(streams_after, streams_before)
            # 标题、作者和原始链接必须还在，媒体库靠它们显示封面和信息
            self.assertEqual(self.tags(sample), tags_before)
            self.assertEqual(tags_before['title'], '样片')
            # 临时文件不留在目录里
            self.assertEqual(
                sorted(p.name for p in Path(directory).iterdir()), ['sample.mp4']
            )


if __name__ == '__main__':
    unittest.main()
