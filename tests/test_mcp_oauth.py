import base64
import hashlib
import json
import os
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import app as app_mod
import mcp_oauth


def _make_pkce_pair(verifier='test-pkce-verifier-string-with-sufficient-entropy-0123456789'):
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')
    return verifier, challenge


def _extract_csrf(html_text):
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_text)
    return match.group(1) if match else ''


class TestMcpOAuth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.oauth_db = os.path.join(self.tmp.name, 'mcp_oauth.sqlite3')
        self.admin_password = 'super-secret-admin-pass'
        self.admin_hash = hashlib.sha256(self.admin_password.encode('utf-8')).hexdigest()

        self.orig_config = app_mod.config.copy()
        self.orig_testing = app_mod.app.testing
        self.orig_secret = app_mod.app.secret_key

        app_mod.app.testing = True
        app_mod.app.secret_key = 'test-flask-secret-key'
        app_mod.config.update({
            'ENABLE_MCP_SERVER': True,
            'ENABLE_MCP_OAUTH': True,
            'MCP_PATH': '/mcp',
            'MCP_ACCESS_TOKEN': '',
            'MCP_PUBLIC_BASE_URL': 'https://yter.example.com',
            'MCP_OAUTH_DB_PATH': self.oauth_db,
            'MCP_OAUTH_ADMIN_PASSWORD_SHA256': self.admin_hash,
            'MCP_OAUTH_ENABLE_CIMD': True,
            'MCP_OAUTH_ALLOWED_REDIRECT_HOSTS': [],
        })
        self.client = app_mod.app.test_client()

    def tearDown(self):
        app_mod.config.clear()
        app_mod.config.update(self.orig_config)
        app_mod.app.testing = self.orig_testing
        app_mod.app.secret_key = self.orig_secret
        self.tmp.cleanup()

    def test_well_known_discovery_endpoints(self):
        resp = self.client.get('/.well-known/oauth-protected-resource/mcp')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['resource'], 'https://yter.example.com/mcp')
        self.assertEqual(data['authorization_servers'], ['https://yter.example.com'])
        self.assertEqual(data['scopes_supported'], ['mcp'])

        resp2 = self.client.get('/.well-known/oauth-authorization-server')
        self.assertEqual(resp2.status_code, 200)
        meta = resp2.get_json()
        self.assertEqual(meta['issuer'], 'https://yter.example.com')
        self.assertEqual(meta['authorization_endpoint'], 'https://yter.example.com/mcp/oauth/authorize')
        self.assertEqual(meta['token_endpoint'], 'https://yter.example.com/mcp/oauth/token')
        self.assertEqual(meta['registration_endpoint'], 'https://yter.example.com/mcp/oauth/register')
        self.assertEqual(meta['code_challenge_methods_supported'], ['S256'])
        self.assertTrue(meta['client_id_metadata_document_supported'])

    def test_dcr_rejects_insecure_redirect_uri(self):
        resp = self.client.post(
            '/mcp/oauth/register',
            json={
                'client_name': 'Bad App',
                'redirect_uris': ['http://evil.example.com/callback'],
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['error'], 'invalid_redirect_uri')

    def test_full_dcr_pkce_refresh_and_revoke_flow(self):
        # 1. DCR 动态注册
        reg_resp = self.client.post(
            '/mcp/oauth/register',
            json={
                'client_name': 'ChatGPT',
                'redirect_uris': ['https://chatgpt.com/connector_platform_oauth_redirect'],
            },
        )
        self.assertEqual(reg_resp.status_code, 201)
        client_id = reg_resp.get_json()['client_id']

        verifier, challenge = _make_pkce_pair()
        auth_query = {
            'client_id': client_id,
            'redirect_uri': 'https://chatgpt.com/connector_platform_oauth_redirect',
            'response_type': 'code',
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
            'scope': 'mcp',
            'state': 'state-123',
            'resource': 'https://yter.example.com/mcp',
        }

        # 2. 打开授权确认页，验证安全头与防点击劫持
        get_auth = self.client.get('/mcp/oauth/authorize', query_string=auth_query)
        self.assertEqual(get_auth.status_code, 200)
        self.assertEqual(get_auth.headers.get('X-Frame-Options'), 'DENY')
        self.assertIn("frame-ancestors 'none'", get_auth.headers.get('Content-Security-Policy', ''))
        self.assertEqual(get_auth.headers.get('Referrer-Policy'), 'no-referrer')
        html = get_auth.get_data(as_text=True)
        self.assertIn('chatgpt.com', html)
        csrf = _extract_csrf(html)
        self.assertTrue(csrf)

        # 3. 错误口令被拒绝（403）
        bad_post = self.client.post(
            '/mcp/oauth/authorize',
            data={
                **auth_query,
                'csrf_token': csrf,
                'decision': 'approve',
                'admin_password': 'wrong-password',
            },
        )
        self.assertEqual(bad_post.status_code, 403)

        # 4. 正确口令同意授权 -> 302 回调并携带 code、state、iss
        ok_post = self.client.post(
            '/mcp/oauth/authorize',
            data={
                **auth_query,
                'csrf_token': csrf,
                'decision': 'approve',
                'admin_password': self.admin_password,
            },
        )
        self.assertEqual(ok_post.status_code, 302)
        location = ok_post.headers['Location']
        parsed = urlsplit(location)
        self.assertEqual(parsed.hostname, 'chatgpt.com')
        qs = parse_qs(parsed.query)
        code = qs['code'][0]
        self.assertEqual(qs['state'][0], 'state-123')
        self.assertEqual(qs['iss'][0], 'https://yter.example.com')

        # 5. 错误 code_verifier 换取 token 失败
        bad_token = self.client.post(
            '/mcp/oauth/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'client_id': client_id,
                'redirect_uri': auth_query['redirect_uri'],
                'code_verifier': 'wrong-verifier-with-enough-length-00000000000000000000',
            },
        )
        self.assertEqual(bad_token.status_code, 400)
        self.assertEqual(bad_token.get_json()['error'], 'invalid_grant')

        # 6. 正确 code_verifier 换取 access_token + refresh_token
        token_resp = self.client.post(
            '/mcp/oauth/token',
            data={
                'grant_type': 'authorization_code',
                'code': code,
                'client_id': client_id,
                'redirect_uri': auth_query['redirect_uri'],
                'code_verifier': verifier,
                'resource': 'https://yter.example.com/mcp',
            },
        )
        self.assertEqual(token_resp.status_code, 200)
        tokens = token_resp.get_json()
        access_token = tokens['access_token']
        refresh_token = tokens['refresh_token']
        self.assertEqual(tokens['token_type'], 'Bearer')

        # 7. Access Token 可直接调用 /mcp
        mcp_resp = self.client.post(
            '/mcp',
            headers={'Authorization': f'Bearer {access_token}'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
        )
        self.assertEqual(mcp_resp.status_code, 200)
        self.assertEqual(mcp_resp.get_json()['result'], {})

        # 8. Refresh Token 轮换：签发新令牌，旧 refresh_token 立即失效
        refreshed = self.client.post(
            '/mcp/oauth/token',
            data={
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
                'client_id': client_id,
            },
        )
        self.assertEqual(refreshed.status_code, 200)
        new_tokens = refreshed.get_json()
        self.assertNotEqual(new_tokens['refresh_token'], refresh_token)

        reuse_old_refresh = self.client.post(
            '/mcp/oauth/token',
            data={
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
                'client_id': client_id,
            },
        )
        self.assertEqual(reuse_old_refresh.status_code, 400)

        # 9. Grant 列表与撤销后立即失效
        grants = mcp_oauth.list_grants(self.oauth_db)
        self.assertEqual(len(grants), 1)
        self.assertTrue(mcp_oauth.revoke_grant(self.oauth_db, grants[0]['id']))

        after_revoke = self.client.post(
            '/mcp',
            headers={'Authorization': f"Bearer {new_tokens['access_token']}"},
            json={'jsonrpc': '2.0', 'id': 2, 'method': 'ping'},
        )
        self.assertEqual(after_revoke.status_code, 401)

    def test_deny_redirects_without_password(self):
        reg = self.client.post(
            '/mcp/oauth/register',
            json={
                'client_name': 'Local Tool',
                'redirect_uris': ['http://127.0.0.1:9999/cb'],
            },
        )
        client_id = reg.get_json()['client_id']
        _, challenge = _make_pkce_pair()
        auth_query = {
            'client_id': client_id,
            'redirect_uri': 'http://127.0.0.1:9999/cb',
            'response_type': 'code',
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
            'state': 'st-deny',
        }
        page = self.client.get('/mcp/oauth/authorize', query_string=auth_query)
        html = page.get_data(as_text=True)
        # 回环地址必须显示本机程序警告
        self.assertIn('本机设备上的一个程序', html)
        csrf = _extract_csrf(html)

        denied = self.client.post(
            '/mcp/oauth/authorize',
            data={**auth_query, 'csrf_token': csrf, 'decision': 'deny'},
        )
        self.assertEqual(denied.status_code, 302)
        qs = parse_qs(urlsplit(denied.headers['Location']).query)
        self.assertEqual(qs['error'][0], 'access_denied')
        self.assertEqual(qs['state'][0], 'st-deny')

    def test_cimd_client_verification_and_ssrf_block(self):
        # 1. 私网地址 CIMD 被 SSRF 防护拦截
        _, challenge = _make_pkce_pair()
        ssrf_resp = self.client.get(
            '/mcp/oauth/authorize',
            query_string={
                'client_id': 'https://127.0.0.1/oauth/client.json',
                'redirect_uri': 'https://claude.ai/api/mcp/auth_callback',
                'response_type': 'code',
                'code_challenge': challenge,
                'code_challenge_method': 'S256',
            },
        )
        self.assertEqual(ssrf_resp.status_code, 400)
        self.assertIn('没能核实这个应用的身份', ssrf_resp.get_data(as_text=True))

        # 2. 合法公网 CIMD（模拟 Claude 发布文档）通过并显示核实域名
        cimd_url = 'https://claude.ai/oauth/client.json'
        fake_doc = json.dumps({
            'client_id': cimd_url,
            'client_name': 'Claude',
            'redirect_uris': ['https://claude.ai/api/mcp/auth_callback'],
            'token_endpoint_auth_method': 'none',
        }).encode('utf-8')
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content.return_value = [fake_doc]

        with patch('mcp_oauth.requests.get', return_value=mock_response):
            ok_resp = self.client.get(
                '/mcp/oauth/authorize',
                query_string={
                    'client_id': cimd_url,
                    'redirect_uri': 'https://claude.ai/api/mcp/auth_callback',
                    'response_type': 'code',
                    'code_challenge': challenge,
                    'code_challenge_method': 'S256',
                },
            )
        self.assertEqual(ok_resp.status_code, 200)
        html = ok_resp.get_data(as_text=True)
        self.assertIn('由 claude.ai 发布', html)

    def test_cli_mcp_grants_and_revoke(self):
        reg = self.client.post(
            '/mcp/oauth/register',
            json={
                'client_name': 'CLI Test Client',
                'redirect_uris': ['https://claude.ai/api/mcp/auth_callback'],
            },
        )
        client_id = reg.get_json()['client_id']
        _, challenge = _make_pkce_pair()
        auth_req = mcp_oauth.resolve_authorize_request(
            self.oauth_db,
            {
                'client_id': client_id,
                'redirect_uri': 'https://claude.ai/api/mcp/auth_callback',
                'response_type': 'code',
                'code_challenge': challenge,
                'code_challenge_method': 'S256',
            },
            app_mod.config,
            'https://yter.example.com/mcp',
            testing=True,
        )
        _, grant_id = mcp_oauth.issue_authorization_code(self.oauth_db, auth_req)

        runner = app_mod.app.test_cli_runner()
        list_res = runner.invoke(args=['mcp-grants'])
        self.assertEqual(list_res.exit_code, 0)
        self.assertIn(grant_id, list_res.output)
        self.assertIn('CLI Test Client', list_res.output)

        revoke_res = runner.invoke(args=['mcp-revoke', grant_id])
        self.assertEqual(revoke_res.exit_code, 0)
        self.assertIn('已撤销授权', revoke_res.output)

        after_res = runner.invoke(args=['mcp-grants'])
        self.assertEqual(after_res.exit_code, 0)
        self.assertIn('没有 MCP OAuth 授权记录', after_res.output)


if __name__ == '__main__':
    unittest.main()
