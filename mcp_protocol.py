#!/usr/bin/env python3
"""MCP 协议层：Streamable HTTP 上的无状态 JSON-RPC 2.0，仅暴露 tools。

对齐 nav-site/server/mcp/protocol.js 的无状态设计：
- 每个 POST 是一条独立请求，返回单个 JSON 对象，不开 SSE、不发会话 ID。
- 同一个端点同时兼容两代协议：
  - 2026-07-28：无握手，版本号放在 params._meta 并镜像到请求头；核对
    MCP-Protocol-Version / Mcp-Method / Mcp-Name 请求头与请求体一致，
    结果附带 resultType="complete" 与 _meta.serverInfo。
  - 2025-11-25 / 2025-06-18 / 2025-03-26：先发 initialize 握手。
"""

import base64
import json
import re


MODERN_VERSIONS = ('2026-07-28',)
LEGACY_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS

# 每次变更 MCP 工具或行为时按语义化版本递增，方便确认客户端是否已刷新到新版本。
SERVER_INFO = {
    'name': 'pyyoutubedl',
    'title': 'PyYoutubeDL',
    'version': '1.0.0',
}

META_VERSION = 'io.modelcontextprotocol/protocolVersion'
META_SERVER_INFO = 'io.modelcontextprotocol/serverInfo'

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022

# 工具定义静态固定，客户端可缓存 1 小时。
TOOLS_TTL_MS = 60 * 60 * 1000

_BASE64_HEADER_RE = re.compile(r'^=\?base64\?(.*)\?=$')


class ToolError(Exception):
    """工具执行失败且原因可直接返回给模型的业务错误。"""


def _rpc_error(req_id, code, message, status=200, data=None):
    error = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    return {'jsonrpc': '2.0', 'id': req_id, 'error': error}, status, {}


def _get_header(headers, name):
    """大小写不敏感地读取请求头，兼容 dict 与 Flask/Werkzeug Headers。"""
    if headers is None:
        return None
    if hasattr(headers, 'get'):
        direct = headers.get(name)
        if direct is not None:
            return direct
    target = name.lower()
    items = headers.items() if hasattr(headers, 'items') else headers
    for key, value in items:
        if isinstance(key, str) and key.lower() == target:
            return value
    return None


def decode_header_value(value):
    """解码请求头里可能出现的 ``=?base64?...?=`` 编码值。"""
    if not isinstance(value, str):
        return None
    match = _BASE64_HEADER_RE.match(value)
    if not match:
        return value
    try:
        raw_bytes = base64.b64decode(match.group(1), validate=True)
        return raw_bytes.decode('utf-8')
    except Exception:
        return None


def tool_view(tool):
    """返回符合 MCP tools/list 规范的单个工具描述。"""
    view = {
        'name': tool['name'],
        'description': tool['description'],
        'inputSchema': tool['inputSchema'],
    }
    if tool.get('title'):
        view['title'] = tool['title']
    if tool.get('annotations'):
        view['annotations'] = tool['annotations']
    return view


def _call_tool(tools, params, context, before_call=None, logger=None):
    if not isinstance(params, dict):
        return None, (INVALID_PARAMS, 'Tool call params must be an object')
    tool_name = params.get('name')
    tool = next((item for item in tools if item['name'] == tool_name), None)
    if tool is None:
        return None, (INVALID_PARAMS, f'Unknown tool: {tool_name}')

    args = params.get('arguments')
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None, (INVALID_PARAMS, 'Tool arguments must be an object')

    try:
        if before_call is not None:
            before_call(tool)
        value = tool['handler'](args, context)
        text = json.dumps(value, ensure_ascii=False)
        return {
            'content': [{'type': 'text', 'text': text}],
            'isError': False,
        }, None
    except ToolError as exc:
        return {
            'content': [{'type': 'text', 'text': str(exc)}],
            'isError': True,
        }, None
    except Exception as exc:
        if logger is not None:
            logger.exception('MCP 工具 %s 执行出错: %s', tool['name'], exc)
        return {
            'content': [{'type': 'text', 'text': '服务器内部错误，请稍后再试'}],
            'isError': True,
        }, None


def _handle_modern(
    headers,
    req_id,
    method,
    params,
    body_version,
    header_version,
    tools,
    context,
    instructions=None,
    before_call=None,
    logger=None,
):
    def mismatch(text):
        return _rpc_error(
            req_id,
            HEADER_MISMATCH,
            f'Header mismatch: {text}',
            status=400,
        )

    probing = (
        method == 'server/discover'
        and body_version is None
        and not header_version
    )
    if not probing:
        if not isinstance(body_version, str):
            return mismatch(f'params._meta["{META_VERSION}"] is required')
        if not header_version:
            return mismatch('MCP-Protocol-Version header is required')
        if header_version != body_version:
            return mismatch(
                f"MCP-Protocol-Version header value '{header_version}' "
                f"does not match body value '{body_version}'"
            )
        if body_version not in MODERN_VERSIONS:
            return _rpc_error(
                req_id,
                UNSUPPORTED_VERSION,
                'Unsupported protocol version',
                status=400,
                data={
                    'supported': list(SUPPORTED_VERSIONS),
                    'requested': body_version,
                },
            )

        header_method = _get_header(headers, 'Mcp-Method')
        if not header_method:
            return mismatch('Mcp-Method header is required')
        if header_method != method:
            return mismatch(
                f"Mcp-Method header value '{header_method}' "
                f"does not match body value '{method}'"
            )

        if method == 'tools/call':
            raw_name = _get_header(headers, 'Mcp-Name')
            if not raw_name:
                return mismatch('Mcp-Name header is required')
            header_name = decode_header_value(raw_name)
            body_name = params.get('name') if isinstance(params, dict) else None
            if header_name != body_name:
                return mismatch(
                    f"Mcp-Name header value '{raw_name}' "
                    f"does not match body value '{body_name}'"
                )

    def complete(result):
        body = {
            'resultType': 'complete',
            **result,
            '_meta': {META_SERVER_INFO: SERVER_INFO},
        }
        return {'jsonrpc': '2.0', 'id': req_id, 'result': body}, 200, {
            'MCP-Protocol-Version': body_version or MODERN_VERSIONS[0],
        }

    if method == 'server/discover':
        result = {
            'supportedVersions': list(SUPPORTED_VERSIONS),
            'capabilities': {'tools': {}},
            'ttlMs': TOOLS_TTL_MS,
            'cacheScope': 'public',
        }
        if instructions:
            result['instructions'] = instructions
        return complete(result)

    if method == 'tools/list':
        return complete({
            'tools': [tool_view(tool) for tool in tools],
            'ttlMs': TOOLS_TTL_MS,
            'cacheScope': 'public',
        })

    if method == 'tools/call':
        result, rpc_err = _call_tool(
            tools,
            params,
            context,
            before_call=before_call,
            logger=logger,
        )
        if rpc_err is not None:
            return _rpc_error(req_id, rpc_err[0], rpc_err[1])
        return complete(result)

    return _rpc_error(
        req_id,
        METHOD_NOT_FOUND,
        f'Method not found: {method}',
        status=404,
    )


def handle_mcp_post(
    raw_body,
    headers,
    tools,
    context,
    instructions=None,
    before_call=None,
    logger=None,
):
    """处理一条发往 MCP 端点的 POST JSON-RPC 2.0 请求。

    Args:
        raw_body: 请求体（bytes / str / 已解析对象）。
        headers: 请求头映射。
        tools: 工具定义列表（顺序固定）。
        context: 透传给各工具 handler 的上下文字典。
        instructions: 给模型看的使用说明。
        before_call: 每次工具调用前执行的回调，可抛 ToolError 拒绝（如限流）。
        logger: 可选日志记录器。

    Returns:
        tuple: ``(body_or_none, status_code, extra_headers)``。
    """
    if isinstance(raw_body, (bytes, bytearray)):
        try:
            text = raw_body.decode('utf-8')
        except UnicodeDecodeError:
            return _rpc_error(None, PARSE_ERROR, 'Parse error', status=400)
        try:
            message = json.loads(text)
        except ValueError:
            return _rpc_error(None, PARSE_ERROR, 'Parse error', status=400)
    elif isinstance(raw_body, str):
        try:
            message = json.loads(raw_body)
        except ValueError:
            return _rpc_error(None, PARSE_ERROR, 'Parse error', status=400)
    else:
        message = raw_body

    if isinstance(message, list):
        return _rpc_error(
            None,
            INVALID_REQUEST,
            'JSON-RPC batching is not supported',
            status=400,
        )
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return _rpc_error(
            None,
            INVALID_REQUEST,
            'Invalid JSON-RPC message',
            status=400,
        )

    req_id = message.get('id')
    method = message.get('method')
    params = message.get('params')

    # 通知（无 id）或客户端响应不返回内容
    if not isinstance(method, str) or 'id' not in message or req_id is None:
        return None, 202, {}

    meta = params.get('_meta') if isinstance(params, dict) else None
    body_version = meta.get(META_VERSION) if isinstance(meta, dict) else None
    header_version = _get_header(headers, 'MCP-Protocol-Version')
    is_modern = (
        isinstance(body_version, str)
        or header_version in MODERN_VERSIONS
        or method == 'server/discover'
    )

    if is_modern:
        return _handle_modern(
            headers=headers,
            req_id=req_id,
            method=method,
            params=params,
            body_version=body_version,
            header_version=header_version,
            tools=tools,
            context=context,
            instructions=instructions,
            before_call=before_call,
            logger=logger,
        )

    if header_version and header_version not in LEGACY_VERSIONS:
        return _rpc_error(
            req_id,
            INVALID_REQUEST,
            f'Unsupported protocol version: {header_version}',
            status=400,
            data={
                'supported': list(SUPPORTED_VERSIONS),
                'requested': header_version,
            },
        )

    if method == 'initialize':
        requested = (
            params.get('protocolVersion')
            if isinstance(params, dict)
            else None
        )
        negotiated = (
            requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        )
        result = {
            'protocolVersion': negotiated,
            'capabilities': {'tools': {}},
            'serverInfo': SERVER_INFO,
        }
        if instructions:
            result['instructions'] = instructions
        return (
            {'jsonrpc': '2.0', 'id': req_id, 'result': result},
            200,
            {'MCP-Protocol-Version': negotiated},
        )

    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': req_id, 'result': {}}, 200, {}

    if method == 'tools/list':
        return (
            {
                'jsonrpc': '2.0',
                'id': req_id,
                'result': {'tools': [tool_view(tool) for tool in tools]},
            },
            200,
            {},
        )

    if method == 'tools/call':
        result, rpc_err = _call_tool(
            tools,
            params,
            context,
            before_call=before_call,
            logger=logger,
        )
        if rpc_err is not None:
            return _rpc_error(req_id, rpc_err[0], rpc_err[1])
        return {'jsonrpc': '2.0', 'id': req_id, 'result': result}, 200, {}

    return _rpc_error(req_id, METHOD_NOT_FOUND, f'Method not found: {method}')
