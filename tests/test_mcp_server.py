import json
import os
import tempfile
import unittest
from unittest.mock import patch

import ai_summary_store
import app as app_mod
import mcp_protocol


class TestMcpServer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.urls_dir = os.path.join(self.tmp.name, 'urls')
        self.files_dir = os.path.join(self.tmp.name, 'files')
        self.log_dir = os.path.join(self.tmp.name, 'logs')
        self.ai_db = os.path.join(self.tmp.name, 'ai_summaries.sqlite3')
        self.oauth_db = os.path.join(self.tmp.name, 'mcp_oauth.sqlite3')
        os.makedirs(self.urls_dir, exist_ok=True)
        os.makedirs(self.files_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        ai_summary_store.init_db(self.ai_db)

        self.orig_config = app_mod.config.copy()
        self.orig_urls_dir = app_mod.URLS_DIR
        self.orig_files_dir = app_mod.FILES_DIR
        self.orig_testing = app_mod.app.testing

        self.token = 'mcp-test-static-token-123456'
        app_mod.app.testing = True
        app_mod.URLS_DIR = self.urls_dir
        app_mod.FILES_DIR = self.files_dir
        app_mod.config.update({
            'URLS_DIR': self.urls_dir,
            'FILES_DIR': self.files_dir,
            'LOG_DIR': self.log_dir,
            'AI_SUMMARY_DB_PATH': self.ai_db,
            'AI_API_BASE_URL': '',
            'AI_API_MODEL': '',
            'AI_API_TOKEN': '',
            'ENABLE_MCP_SERVER': True,
            'ENABLE_MCP_OAUTH': True,
            'MCP_PATH': '/mcp',
            'MCP_ACCESS_TOKEN': self.token,
            'MCP_PUBLIC_BASE_URL': 'https://yter.example.com',
            'MCP_MAX_PENDING_TASKS': 5,
            'MCP_RATE_LIMIT_PER_MINUTE': 120,
            'MCP_WRITE_RATE_LIMIT_PER_MINUTE': 30,
            'MCP_OAUTH_DB_PATH': self.oauth_db,
            'MCP_OAUTH_ADMIN_PASSWORD_SHA256': 'a' * 64,
            'PLAYLIST_MAX_ITEMS': 10,
            'PLAYLIST_AUDIO_MAX_ITEMS': 20,
            'PLAYER_FILENAME_EXCLUDE_KEYWORDS': ['_secret_'],
        })
        app_mod.app.mcp_rate_limiter.clear()
        self.client = app_mod.app.test_client()

    def tearDown(self):
        app_mod.config.clear()
        app_mod.config.update(self.orig_config)
        app_mod.URLS_DIR = self.orig_urls_dir
        app_mod.FILES_DIR = self.orig_files_dir
        app_mod.app.testing = self.orig_testing
        app_mod.app.mcp_rate_limiter.clear()
        self.tmp.cleanup()

    def _call_tool(self, name, arguments=None, token=None):
        auth_token = self.token if token is None else token
        resp = self.client.post(
            '/mcp',
            headers={'Authorization': f'Bearer {auth_token}'},
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'tools/call',
                'params': {'name': name, 'arguments': arguments or {}},
            },
        )
        self.assertEqual(resp.status_code, 200)
        result = resp.get_json()['result']
        text = result['content'][0]['text']
        if result['isError']:
            return True, text
        return False, json.loads(text)

    def test_disabled_returns_503(self):
        app_mod.config['ENABLE_MCP_SERVER'] = False
        resp = self.client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'})
        self.assertEqual(resp.status_code, 503)

    def test_unauthenticated_returns_401_with_resource_metadata(self):
        for method in ('GET', 'POST'):
            resp = self.client.open('/mcp', method=method)
            self.assertEqual(resp.status_code, 401)
            www_auth = resp.headers.get('WWW-Authenticate', '')
            self.assertIn(
                'resource_metadata="https://yter.example.com/.well-known/oauth-protected-resource/mcp"',
                www_auth,
            )

    def test_authenticated_get_and_delete_return_405(self):
        headers = {'Authorization': f'Bearer {self.token}'}
        for method in ('GET', 'DELETE'):
            resp = self.client.open('/mcp', method=method, headers=headers)
            self.assertEqual(resp.status_code, 405)
            self.assertEqual(resp.headers.get('Allow'), 'POST')

    def test_tools_list_fixed_order_and_annotations(self):
        resp = self.client.post(
            '/mcp',
            headers={'Authorization': f'Bearer {self.token}'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
        )
        self.assertEqual(resp.status_code, 200)
        tools = resp.get_json()['result']['tools']
        self.assertEqual(
            [t['name'] for t in tools],
            [
                'get_video_info',
                'download_media',
                'get_task_status',
                'list_recent_tasks',
                'list_media_files',
                'get_media_subtitles',
                'get_video_summary',
                'get_service_status',
            ],
        )
        by_name = {t['name']: t for t in tools}
        self.assertTrue(by_name['get_video_info']['annotations']['readOnlyHint'])
        self.assertFalse(by_name['download_media']['annotations']['readOnlyHint'])

    def test_download_media_single_url_ssrf_and_queue_limit(self):
        # 1. 内网地址被 SSRF 拦截
        is_err, msg = self._call_tool(
            'download_media',
            {'url': 'http://127.0.0.1:8080/internal.mp4', 'types': ['video']},
        )
        self.assertTrue(is_err)
        self.assertIn('安全策略拒绝', msg)

        # 2. 分享文本提取 URL 并同时下视频与音频
        is_err, data = self._call_tool(
            'download_media',
            {
                'url': '【好视频】 https://www.youtube.com/watch?v=dQw4w9WgXcQ 推荐看',
                'types': ['video', 'audio'],
            },
        )
        self.assertFalse(is_err)
        self.assertTrue(data['created'])
        self.assertEqual(data['task_count'], 2)
        self.assertEqual(len(data['task_ids']), 2)
        for task_id in data['task_ids']:
            self.assertTrue(os.path.isfile(os.path.join(self.urls_dir, f'{task_id}.txt')))

        # 3. 队列积压达到 MCP_MAX_PENDING_TASKS=5 时拒绝继续入队
        for i in range(3):
            with open(os.path.join(self.urls_dir, f'v2026112116000{i}abc.txt'), 'w', encoding='utf-8') as f:
                f.write('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        is_err, msg = self._call_tool(
            'download_media',
            {'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'types': ['video']},
        )
        self.assertTrue(is_err)
        self.assertIn('上限 5', msg)

    def test_download_media_playlist_requires_confirmation_by_default(self):
        playlist_url = 'https://www.youtube.com/playlist?list=PL1234567890'
        preview_list = [
            'https://www.youtube.com/watch?v=aaaaaaaaaaa',
            'https://www.youtube.com/watch?v=bbbbbbbbbbb',
        ]
        with patch.object(app_mod, 'resolve_playlist_urls', return_value=(preview_list, None)):
            # 默认 allow_playlist=false：只返回预览，不写任务文件
            is_err, preview = self._call_tool(
                'download_media',
                {'url': playlist_url, 'types': ['audio']},
            )
            self.assertFalse(is_err)
            self.assertFalse(preview['created'])
            self.assertTrue(preview['requires_confirmation'])
            self.assertEqual(preview['preview_count'], 2)
            self.assertEqual(os.listdir(self.urls_dir), [])

            # 明确确认 allow_playlist=true + max_items=1：只创建 1 条音频任务
            is_err, created = self._call_tool(
                'download_media',
                {
                    'url': playlist_url,
                    'types': ['audio'],
                    'allow_playlist': True,
                    'max_items': 1,
                },
            )
            self.assertFalse(is_err)
            self.assertTrue(created['created'])
            self.assertEqual(created['task_count'], 1)
            self.assertTrue(created['task_ids'][0].startswith('a'))

    def test_get_task_status_completed_urls_and_failed_log_redaction(self):
        # 构造已完成任务与产物
        ok_id = 'v20261121150000abc'
        video_file = 'demo-video.mp4'
        with open(os.path.join(self.files_dir, video_file), 'wb') as f:
            f.write(b'fake-mp4-bytes')
        with open(os.path.join(self.urls_dir, f'{ok_id}.ok'), 'w', encoding='utf-8') as f:
            f.write('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
        with open(os.path.join(self.urls_dir, f'{ok_id}.result.json'), 'w', encoding='utf-8') as f:
            json.dump({'files': [video_file]}, f)

        # 构造失败任务与含项目绝对路径的日志
        fail_id = 'a20261121150100xyz'
        with open(os.path.join(self.urls_dir, f'{fail_id}.fail'), 'w', encoding='utf-8') as f:
            f.write('https://www.youtube.com/watch?v=deadbeef123')
        project_root = os.path.abspath(os.path.dirname(app_mod.__file__))
        with open(os.path.join(self.log_dir, f'{fail_id}.log'), 'w', encoding='utf-8') as f:
            f.write(f'running in {project_root}/tmp/{fail_id}\nERROR: Sign in to confirm you are not a bot\n')

        is_err, result = self._call_tool('get_task_status', {'task_ids': [ok_id, fail_id]})
        self.assertFalse(is_err)
        by_id = {item['task']: item for item in result['tasks']}

        self.assertEqual(by_id[ok_id]['state'], 'completed')
        self.assertTrue(by_id[ok_id]['player_url'].startswith('https://yter.example.com/player'))
        self.assertTrue(by_id[ok_id]['download_url'].startswith('https://yter.example.com/downloads/'))

        self.assertEqual(by_id[fail_id]['state'], 'failed')
        self.assertIn('Sign in to confirm', by_id[fail_id]['error_summary'])
        self.assertNotIn(project_root, by_id[fail_id]['log_tail'])

        # list_recent_tasks 可按状态筛选
        is_err, recent = self._call_tool('list_recent_tasks', {'state': 'failed', 'limit': 5})
        self.assertFalse(is_err)
        self.assertEqual(recent['returned'], 1)
        self.assertEqual(recent['tasks'][0]['task'], fail_id)

    def test_list_media_files_and_get_media_subtitles(self):
        with open(os.path.join(self.files_dir, 'lesson.mp4'), 'wb') as f:
            f.write(b'mp4data')
        with open(os.path.join(self.files_dir, 'lesson.zh-Hans.vtt'), 'w', encoding='utf-8') as f:
            f.write('WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n你好，欢迎来到课程。\n')
        with open(os.path.join(self.files_dir, 'clip_secret_hidden.mp4'), 'wb') as f:
            f.write(b'hidden')

        is_err, listing = self._call_tool('list_media_files', {'media_type': 'video'})
        self.assertFalse(is_err)
        names = [item['filename'] for item in listing['files']]
        self.assertEqual(names, ['lesson.mp4'])
        self.assertEqual(listing['files'][0]['subtitle_track_count'], 1)
        self.assertTrue(listing['files'][0]['player_url'].startswith('https://yter.example.com/player'))

        with patch.object(app_mod, 'extract_sidecar_subtitle_text', return_value='你好，欢迎来到课程。'):
            is_err, sub = self._call_tool('get_media_subtitles', {'filename': 'lesson.mp4'})
        self.assertFalse(is_err)
        self.assertEqual(sub['text'], '你好，欢迎来到课程。')
        self.assertFalse(sub['truncated'])

    def test_get_video_summary_unconfigured_and_cached(self):
        # 未配置 AI 接口时返回清晰提示
        is_err, msg = self._call_tool(
            'get_video_summary',
            {'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'},
        )
        self.assertTrue(is_err)
        self.assertIn('get_media_subtitles', msg)

        # 配置 AI 接口后命中或创建异步任务（模拟公网 DNS 解析，避免受本机代理 Fake-IP 影响）
        app_mod.config.update({
            'AI_API_BASE_URL': 'https://llm.example.com/v1/chat/completions',
            'AI_API_MODEL': 'demo-model',
            'AI_API_TOKEN': 'sk-demo',
        })
        fake_addrinfo = [(2, 1, 6, '', ('93.184.216.34', 0))]
        with patch('ai_summary_store.socket.getaddrinfo', return_value=fake_addrinfo):
            is_err, job_res = self._call_tool(
                'get_video_summary',
                {'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'},
            )
        self.assertFalse(is_err)
        self.assertEqual(job_res['status'], 'queued')
        self.assertTrue(job_res['job_id'])

        # 按 job_id 轮询
        is_err, poll_res = self._call_tool('get_video_summary', {'job_id': job_res['job_id']})
        self.assertFalse(is_err)
        self.assertEqual(poll_res['job_id'], job_res['job_id'])

    def test_get_service_status_does_not_leak_secrets(self):
        is_err, status = self._call_tool('get_service_status')
        self.assertFalse(is_err)
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertNotIn(self.token, serialized)
        self.assertNotIn(self.tmp.name, serialized)
        self.assertEqual(status['server'], mcp_protocol.SERVER_INFO)

    def test_rate_limiting_returns_tool_error(self):
        app_mod.config['MCP_RATE_LIMIT_PER_MINUTE'] = 1
        is_err1, _ = self._call_tool('get_service_status')
        self.assertFalse(is_err1)
        is_err2, msg2 = self._call_tool('get_service_status')
        self.assertTrue(is_err2)
        self.assertIn('过于频繁', msg2)


if __name__ == '__main__':
    unittest.main()
