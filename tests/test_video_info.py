import unittest
import json
import subprocess
import sys
from concurrent.futures import Future
from unittest.mock import patch

import app as app_module
from app import app

class TestVideoInfoAPI(unittest.TestCase):
    def setUp(self):
        # 创建测试客户端
        self.app = app.test_client()
        # 设置测试模式
        self.app.testing = True
        with app_module.BASIC_VIDEO_INFO_JOBS_LOCK:
            app_module.BASIC_VIDEO_INFO_JOBS.clear()

    def test_missing_url(self):
        """测试缺少url参数的情况"""
        response = self.app.post('/api/video_info')
        data = json.loads(response.data)
        
        self.assertEqual(response.status_code, 400)
        self.assertFalse(data['success'])
        self.assertEqual(data['msg'], 'Missing required parameter: url')

    def test_invalid_url(self):
        """测试无效的URL"""
        response = self.app.post('/api/video_info',
                               json={'url': 'https://www.youtube.com/watch?v=invalid'})
        data = json.loads(response.data)
        
        self.assertEqual(response.status_code, 500)
        self.assertFalse(data['success'])
        self.assertTrue('Failed to get video info' in data['msg'])

    @patch('app.subprocess.run')
    def test_video_info_disables_configured_sleep(self, run):
        """视频信息查询应覆盖下载配置中的所有常规sleep选项。"""
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({'title': 'test', 'formats': []}),
            stderr='',
        )

        response = self.app.post(
            '/api/video_info',
            json={'url': 'https://www.youtube.com/watch?v=test'},
        )

        self.assertEqual(response.status_code, 200)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], [sys.executable, '-m', 'yt_dlp'])
        for option in (
            '--sleep-requests',
            '--sleep-interval',
            '--max-sleep-interval',
            '--sleep-subtitles',
        ):
            index = cmd.index(option)
            self.assertEqual(cmd[index + 1], '0')
        self.assertLess(cmd.index('--config-location'), cmd.index('--sleep-requests'))
        self.assertEqual(cmd[-1], 'https://www.youtube.com/watch?v=test')

    @patch('app.subprocess.run')
    def test_video_info_uploader_falls_back_to_platform(self, run):
        """作者相关字段缺失时，应显示yt-dlp识别的平台名称。"""
        cases = [
            (
                {'uploader': '作者名称', 'extractor_key': 'TikTok'},
                '作者名称',
            ),
            (
                {'uploader': None, 'channel': None, 'extractor_key': 'TikTok'},
                'TikTok',
            ),
            (
                {'uploader': None, 'extractor': 'BiliBili'},
                'BiliBili',
            ),
        ]

        for info, expected in cases:
            with self.subTest(expected=expected):
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=json.dumps({
                        'title': 'test',
                        'formats': [],
                        **info,
                    }),
                    stderr='',
                )

                response = self.app.post(
                    '/api/video_info',
                    json={'url': 'https://example.com/video'},
                )
                data = response.get_json()

                self.assertEqual(response.status_code, 200)
                self.assertEqual(data['uploader'], expected)

    @patch('app.subprocess.run')
    def test_basic_video_info_returns_only_preview_fields(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='"测试标题"\t"测试作者"\t607\t"https://img.example/thumb.jpg"\n',
            stderr='',
        )

        response = self.app.post(
            '/api/video_info_basic',
            json={'url': 'https://www.youtube.com/watch?v=test'},
        )
        submitted = response.get_json()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(submitted['status'], 'pending')
        self.assertEqual(response.headers['Retry-After'], '1')
        job = app_module.BASIC_VIDEO_INFO_JOBS[submitted['job_id']]
        job['future'].result(timeout=1)

        poll_response = self.app.get(submitted['poll_url'])
        data = poll_response.get_json()
        self.assertEqual(poll_response.status_code, 200)
        self.assertEqual(data['status'], 'completed')
        self.assertEqual({
            key: data[key]
            for key in ('success', 'title', 'uploader', 'duration', 'thumbnail')
        }, {
            'success': True,
            'title': '测试标题',
            'uploader': '测试作者',
            'duration': 607,
            'thumbnail': 'https://img.example/thumb.jpg',
        })
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:3], [sys.executable, '-m', 'yt_dlp'])
        self.assertIn('--print', cmd)
        self.assertIn('%(title)j\t%(uploader)j\t%(duration)j\t%(thumbnail)j', cmd)
        self.assertIn('--no-write-subs', cmd)
        self.assertIn('--no-write-auto-subs', cmd)
        self.assertIn('--no-playlist', cmd)
        self.assertNotIn('--dump-single-json', cmd)
        self.assertEqual(
            run.call_args.kwargs['timeout'],
            app_module.BASIC_VIDEO_INFO_PROCESS_TIMEOUT_SECONDS,
        )

    def test_basic_video_info_submit_and_poll_do_not_wait_for_worker(self):
        future = Future()
        with patch.object(
            app_module.BASIC_VIDEO_INFO_EXECUTOR,
            'submit',
            return_value=future,
        ):
            response = self.app.post(
                '/api/video_info_basic',
                json={'url': 'https://www.youtube.com/watch?v=pending'},
            )

        self.assertEqual(response.status_code, 202)
        submitted = response.get_json()
        poll_response = self.app.get(submitted['poll_url'])
        self.assertEqual(poll_response.status_code, 202)
        self.assertEqual(poll_response.get_json()['status'], 'pending')

        future.set_result({
            'title': '完成标题',
            'uploader': '完成作者',
            'duration': 12,
            'thumbnail': None,
        })
        poll_response = self.app.get(submitted['poll_url'])
        self.assertEqual(poll_response.status_code, 200)
        self.assertEqual(poll_response.get_json()['title'], '完成标题')

    def test_basic_video_info_poll_returns_worker_error(self):
        future = Future()
        future.set_exception(RuntimeError('extractor failed'))
        with patch.object(
            app_module.BASIC_VIDEO_INFO_EXECUTOR,
            'submit',
            return_value=future,
        ):
            response = self.app.post(
                '/api/video_info_basic',
                json={'url': 'https://example.com/failed-video'},
            )

        poll_response = self.app.get(response.get_json()['poll_url'])
        self.assertEqual(poll_response.status_code, 500)
        self.assertEqual(poll_response.get_json()['status'], 'failed')
        self.assertIn('extractor failed', poll_response.get_json()['msg'])

    def test_basic_video_info_poll_rejects_unknown_job(self):
        response = self.app.get('/api/video_info_basic/jobs/missing')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()['status'], 'missing')

    def test_basic_video_info_rejects_when_workers_are_busy(self):
        futures = [Future(), Future()]
        with patch.object(
            app_module.BASIC_VIDEO_INFO_EXECUTOR,
            'submit',
            side_effect=futures,
        ):
            for index in range(app_module.BASIC_VIDEO_INFO_MAX_PENDING_JOBS):
                response = self.app.post(
                    '/api/video_info_basic',
                    json={'url': f'https://example.com/video-{index}'},
                )
                self.assertEqual(response.status_code, 202)

            response = self.app.post(
                '/api/video_info_basic',
                json={'url': 'https://example.com/one-too-many'},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers['Retry-After'], '2')
        self.assertIn('busy', response.get_json()['msg'])

    def test_valid_video(self):
        """测试有效的视频URL"""
        # 使用一个已知存在的视频
        test_url = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
        response = self.app.post('/api/video_info',
                               json={'url': test_url})
        data = json.loads(response.data)
        
        # 检查响应状态
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])
        
        # 检查返回的数据结构
        self.assertIn('title', data)
        self.assertIn('description', data)
        self.assertIn('duration', data)
        self.assertIn('uploader', data)
        self.assertIn('formats', data)
        self.assertIn('audio_formats', data)
        
        # 检查视频格式信息
        self.assertTrue(len(data['formats']) > 0)
        format = data['formats'][0]
        self.assertIn('format_id', format)
        self.assertIn('ext', format)
        self.assertIn('resolution', format)
        
        # 检查音频格式信息
        self.assertTrue(len(data['audio_formats']) > 0)
        audio_format = data['audio_formats'][0]
        self.assertIn('format_id', audio_format)
        self.assertIn('ext', audio_format)
        self.assertIn('acodec', audio_format)

    def test_form_data(self):
        """测试使用form-data格式提交"""
        test_url = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
        response = self.app.post('/api/video_info',
                               data={'url': test_url})
        data = json.loads(response.data)
        
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['success'])

if __name__ == '__main__':
    unittest.main()
