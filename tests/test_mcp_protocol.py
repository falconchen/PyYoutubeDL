import base64
import json
import unittest

import mcp_protocol


class TestMcpProtocol(unittest.TestCase):
    def setUp(self):
        self.tools = [
            {
                'name': 'echo',
                'title': '回显',
                'description': '原样返回输入',
                'inputSchema': {
                    'type': 'object',
                    'properties': {'text': {'type': 'string'}},
                    'required': ['text'],
                },
                'annotations': {'readOnlyHint': True},
                'handler': lambda args, ctx: {'echo': args.get('text'), 'who': ctx.get('who')},
            },
            {
                'name': 'fail_business',
                'description': '抛出业务错误',
                'inputSchema': {'type': 'object', 'properties': {}},
                'handler': self._raise_tool_error,
            },
            {
                'name': 'fail_internal',
                'description': '抛出未捕获异常',
                'inputSchema': {'type': 'object', 'properties': {}},
                'handler': self._raise_runtime_error,
            },
        ]

    @staticmethod
    def _raise_tool_error(args, ctx):
        del args, ctx
        raise mcp_protocol.ToolError('播放列表为空')

    @staticmethod
    def _raise_runtime_error(args, ctx):
        del args, ctx
        raise RuntimeError('secret internal path /srv/private')

    def test_invalid_json_and_batch_rejected(self):
        body, status, _ = mcp_protocol.handle_mcp_post('{bad', {}, self.tools, {})
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], mcp_protocol.PARSE_ERROR)

        body, status, _ = mcp_protocol.handle_mcp_post('[{"jsonrpc":"2.0"}]', {}, self.tools, {})
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], mcp_protocol.INVALID_REQUEST)

    def test_notification_returns_202(self):
        payload = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        body, status, _ = mcp_protocol.handle_mcp_post(payload, {}, self.tools, {})
        self.assertIsNone(body)
        self.assertEqual(status, 202)

    def test_legacy_initialize_ping_list_and_call(self):
        init_req = json.dumps({
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {'protocolVersion': '2025-06-18'},
        })
        body, status, headers = mcp_protocol.handle_mcp_post(
            init_req,
            {},
            self.tools,
            {},
            instructions='测试说明',
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('MCP-Protocol-Version'), '2025-06-18')
        self.assertEqual(body['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(body['result']['serverInfo'], mcp_protocol.SERVER_INFO)
        self.assertEqual(body['result']['instructions'], '测试说明')

        ping_req = json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'ping'})
        body, status, _ = mcp_protocol.handle_mcp_post(ping_req, {}, self.tools, {})
        self.assertEqual(status, 200)
        self.assertEqual(body['result'], {})

        list_req = json.dumps({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'})
        body, status, _ = mcp_protocol.handle_mcp_post(list_req, {}, self.tools, {})
        self.assertEqual(status, 200)
        self.assertEqual([t['name'] for t in body['result']['tools']], ['echo', 'fail_business', 'fail_internal'])
        self.assertEqual(body['result']['tools'][0]['annotations'], {'readOnlyHint': True})

        call_req = json.dumps({
            'jsonrpc': '2.0',
            'id': 4,
            'method': 'tools/call',
            'params': {'name': 'echo', 'arguments': {'text': 'hi'}},
        })
        body, status, _ = mcp_protocol.handle_mcp_post(call_req, {}, self.tools, {'who': 'tester'})
        self.assertEqual(status, 200)
        self.assertFalse(body['result']['isError'])
        parsed = json.loads(body['result']['content'][0]['text'])
        self.assertEqual(parsed, {'echo': 'hi', 'who': 'tester'})

    def test_tool_error_vs_internal_error_redaction(self):
        req_biz = json.dumps({
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'tools/call',
            'params': {'name': 'fail_business', 'arguments': {}},
        })
        body, status, _ = mcp_protocol.handle_mcp_post(req_biz, {}, self.tools, {})
        self.assertEqual(status, 200)
        self.assertTrue(body['result']['isError'])
        self.assertEqual(body['result']['content'][0]['text'], '播放列表为空')

        req_int = json.dumps({
            'jsonrpc': '2.0',
            'id': 2,
            'method': 'tools/call',
            'params': {'name': 'fail_internal', 'arguments': {}},
        })
        body, status, _ = mcp_protocol.handle_mcp_post(req_int, {}, self.tools, {})
        self.assertEqual(status, 200)
        self.assertTrue(body['result']['isError'])
        self.assertNotIn('/srv/private', body['result']['content'][0]['text'])

    def test_modern_discover_and_header_validation(self):
        discover_probe = json.dumps({'jsonrpc': '2.0', 'id': 'd1', 'method': 'server/discover'})
        body, status, _ = mcp_protocol.handle_mcp_post(
            discover_probe,
            {},
            self.tools,
            {},
            instructions='现代协议说明',
        )
        self.assertEqual(status, 200)
        self.assertEqual(body['result']['resultType'], 'complete')
        self.assertIn('2026-07-28', body['result']['supportedVersions'])
        self.assertEqual(
            body['result']['_meta'][mcp_protocol.META_SERVER_INFO],
            mcp_protocol.SERVER_INFO,
        )

        # 缺失请求头返回 400 + -32020
        bad_call = json.dumps({
            'jsonrpc': '2.0',
            'id': 'm1',
            'method': 'tools/call',
            'params': {
                '_meta': {mcp_protocol.META_VERSION: '2026-07-28'},
                'name': 'echo',
                'arguments': {'text': 'hello'},
            },
        })
        body, status, _ = mcp_protocol.handle_mcp_post(bad_call, {}, self.tools, {})
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], mcp_protocol.HEADER_MISMATCH)

        # 带合法头（且 Mcp-Name 使用 =?base64?...?= 编码）通过
        encoded_name = '=?base64?' + base64.b64encode(b'echo').decode('ascii') + '?='
        headers = {
            'MCP-Protocol-Version': '2026-07-28',
            'Mcp-Method': 'tools/call',
            'Mcp-Name': encoded_name,
        }
        body, status, resp_headers = mcp_protocol.handle_mcp_post(
            bad_call,
            headers,
            self.tools,
            {'who': 'modern'},
        )
        self.assertEqual(status, 200)
        self.assertEqual(resp_headers.get('MCP-Protocol-Version'), '2026-07-28')
        self.assertEqual(body['result']['resultType'], 'complete')
        self.assertEqual(
            json.loads(body['result']['content'][0]['text']),
            {'echo': 'hello', 'who': 'modern'},
        )

    def test_modern_unsupported_version_and_unknown_method(self):
        unsupported = json.dumps({
            'jsonrpc': '2.0',
            'id': 'u1',
            'method': 'tools/list',
            'params': {'_meta': {mcp_protocol.META_VERSION: '2099-01-01'}},
        })
        body, status, _ = mcp_protocol.handle_mcp_post(
            unsupported,
            {'MCP-Protocol-Version': '2099-01-01', 'Mcp-Method': 'tools/list'},
            self.tools,
            {},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body['error']['code'], mcp_protocol.UNSUPPORTED_VERSION)

        unknown = json.dumps({
            'jsonrpc': '2.0',
            'id': 'u2',
            'method': 'resources/list',
            'params': {'_meta': {mcp_protocol.META_VERSION: '2026-07-28'}},
        })
        body, status, _ = mcp_protocol.handle_mcp_post(
            unknown,
            {'MCP-Protocol-Version': '2026-07-28', 'Mcp-Method': 'resources/list'},
            self.tools,
            {},
        )
        self.assertEqual(status, 404)
        self.assertEqual(body['error']['code'], mcp_protocol.METHOD_NOT_FOUND)


if __name__ == '__main__':
    unittest.main()
