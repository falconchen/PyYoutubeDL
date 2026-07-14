import unittest
import json
import subprocess
from unittest.mock import patch

from app import app

class TestVideoInfoAPI(unittest.TestCase):
    def setUp(self):
        # 创建测试客户端
        self.app = app.test_client()
        # 设置测试模式
        self.app.testing = True

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
