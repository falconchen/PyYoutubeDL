# MCP 接入指南

`PyYoutubeDL` 内置了零额外依赖的 **MCP (Model Context Protocol)** 服务，直接挂载在 Flask 应用的 `/mcp` 路径下，无需新开端口。

接上之后，可以直接让 Claude、ChatGPT、Cursor、Antigravity 等 AI 工具帮你：

- 「帮我看看这个视频有多长、有什么清晰度和字幕：`<链接>`」
- 「把这个视频下载成音频（或视频 + 音频）」
- 「刚才提交的下载好了吗？给我播放链接和下载链接」
- 「下载失败了的话帮我看看报错原因」
- 「把本地已经下好的那个视频字幕读出来总结一下，或者直接调 AI 总结」

---

## 一、 服务端配置（`config.json`）

在未提交到 Git 的 `config.json` 中按需开启以下配置项（切勿把真实令牌或密码哈希写入公开仓库）：

```jsonc
{
  // 必须配置稳定密钥（OAuth 授权页 CSRF 校验依赖 Flask session）
  "FLASK_SECRET_KEY": "替换为长随机字符串",

  // 开启 MCP 服务
  "ENABLE_MCP_SERVER": true,
  "MCP_PATH": "/mcp",
  // 静态 Bearer 令牌（可选，供 Claude Code / Cursor / Antigravity 等配置请求头）
  "MCP_ACCESS_TOKEN": "替换为长随机令牌",
  // 对外公网 HTTPS 地址（Claude / ChatGPT 网页版要求标准 443 端口 HTTPS）
  "MCP_PUBLIC_BASE_URL": "https://yter.example.com",
  "MCP_MAX_PENDING_TASKS": 50,

  // 开启内置 OAuth 2.1（供 Claude / ChatGPT 网页版添加自定义 MCP 连接器）
  "ENABLE_MCP_OAUTH": true,
  "MCP_OAUTH_DB_PATH": "./data/mcp_oauth.sqlite3",
  // 授权确认页管理员口令的 sha256 十六进制哈希（禁止填明文）
  "MCP_OAUTH_ADMIN_PASSWORD_SHA256": "通过下方命令生成的64位十六进制哈希",
  "MCP_OAUTH_ENABLE_CIMD": true,
  "MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS": 3600,
  "MCP_OAUTH_REFRESH_TOKEN_IDLE_TTL_SECONDS": 7776000,
  "MCP_OAUTH_ALLOWED_REDIRECT_HOSTS": [],
}
```

生成管理员口令哈希与随机令牌：

```bash
# 生成授权确认页口令的 sha256 哈希（把“你的口令”换成强密码）
python -c "import hashlib; print(hashlib.sha256(b'你的口令').hexdigest())"

# 生成 32 字节随机静态令牌（MCP_ACCESS_TOKEN）
python -c "import secrets; print('pydl_mcp_' + secrets.token_urlsafe(32))"
```

---

## 二、 Nginx / OpenResty 反向代理配置

如果站点前面有 Nginx / OpenResty，并且配置了**整站 Basic Auth**，**必须**为 MCP 端点、OAuth 端点和 `.well-known` 发现端点单独关闭 Basic Auth（由 MCP 自身的 OAuth 2.1 / Bearer Token 接管认证），否则 Claude / ChatGPT 网页版会被反代层 401 拦住：

```nginx
# MCP 端点与 OAuth 2.1 发现/授权路径（与整站 Basic Auth 隔离）
location = /mcp {
    auth_basic off;
    proxy_pass http://127.0.0.1:5100;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 120s;
}

location ^~ /mcp/oauth/ {
    auth_basic off;
    proxy_pass http://127.0.0.1:5100;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}

location ^~ /.well-known/oauth- {
    auth_basic off;
    proxy_pass http://127.0.0.1:5100;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}

location = /.well-known/openid-configuration {
    auth_basic off;
    proxy_pass http://127.0.0.1:5100;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

> 注意：Claude 网页版要求标准 HTTPS **443** 端口，且拒绝自定义端口（如 `:8443`）。

---

## 三、 客户端接入方式

### 1. Claude 网页版 / 手机 App（OAuth 2.1 + CIMD）

1. 打开 `claude.ai` → **Customize** → **Connectors** → **+ Add** → **Add custom connector**。
2. 名称填 `PyYoutubeDL`，远程地址填 `https://<你的域名>/mcp`。
3. 认证方式保持默认（**Sign in now** + **Use Claude's published identity**）。
4. 浏览器会打开本站授权页，显示「由 `claude.ai` 发布（身份已核实）」及回调域名，核对无误后输入管理员授权口令并点击「同意授权」。
5. 对话时点击输入框左下角 `+` → Connectors 开启即可使用。

### 2. ChatGPT 网页版（OAuth 2.1 + DCR / CIMD）

1. 打开 `chatgpt.com` → **设置 / 应用（插件）** → **添加自定义 MCP 服务器**。
2. 名称填 `PyYoutubeDL`，服务器 URL 填 `https://<你的域名>/mcp`，身份验证选择 **OAuth**。
3. 如默认 CIMD 方式遇到「没能核实这个应用的身份」（部分环境 `chatgpt.com` 会拒绝服务端拉取其元数据文档），请展开 **高级 OAuth 设置**：
   - 将 **注册方法** 改为 **动态客户端注册（DCR）**
   - **基础授权范围（Scope）** 填 `mcp`
4. 点击创建并在弹出的本站授权页中核对回调域名为 `chatgpt.com`，输入管理员授权口令并点「同意授权」。

> 访问令牌有效期 1 小时，客户端会自动使用刷新令牌续期；连续闲置 90 天才会失效。

### 3. 本地与开发工具（静态 Bearer Token 或 OAuth）

**Claude Code**：

```bash
claude mcp add --transport http pyyoutubedl https://<你的域名>/mcp \
  --header "Authorization: Bearer <你的 MCP_ACCESS_TOKEN>"
```

**Cursor / Antigravity 等 JSON 配置**：

```json
{
  "mcpServers": {
    "pyyoutubedl": {
      "url": "https://<你的域名>/mcp",
      "headers": {
        "Authorization": "Bearer <你的 MCP_ACCESS_TOKEN>"
      }
    }
  }
}
```

**Claude Desktop（经 `mcp-remote` 桥接）**：

```json
{
  "mcpServers": {
    "pyyoutubedl": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://<你的域名>/mcp",
        "--header",
        "Authorization: Bearer <你的 MCP_ACCESS_TOKEN>"
      ]
    }
  }
}
```

---

## 四、 提供的 MCP 工具

工具顺序保持固定（客户端和模型提示词缓存依赖该顺序）：

| 工具名 | 只读/写入 | 作用 |
| :--- | :--- | :--- |
| `get_video_info` | 只读 | 下载前解析视频/播放列表信息（标题、时长、格式、字幕语言；支持分享文本） |
| `download_media` | 写入 | 提交视频/音频下载任务；**播放列表默认 `allow_playlist=false` 先返回预览要求确认** |
| `get_task_status` | 只读 | 批量查询 1~20 个任务状态与进度，完成后返回播放/下载链接，失败时附脱敏错误摘要 |
| `list_recent_tasks` | 只读 | 按时间倒序查看最近下载任务，支持按状态筛选 |
| `list_media_files` | 只读 | 列出本地已下载的视频/音频文件，支持关键词筛选并附播放/下载链接 |
| `get_media_subtitles` | 只读 | 读取本地已下载媒体的外挂或内嵌纯文本字幕 |
| `get_video_summary` | 写入/查询 | 查询或触发后台 AI 总结任务（支持 `url`、`filename` 或 `job_id`） |
| `get_service_status` | 只读 | 查看下载器等后台 worker 运行状态、队列积压数和系统配置摘要 |

---

## 五、 授权查看与撤销（CLI）

在项目根目录（激活虚拟环境后）执行：

```bash
# 查看当前所有有效的 OAuth 授权应用
FLASK_APP=app.py flask mcp-grants

# 同时查看已撤销的历史记录
FLASK_APP=app.py flask mcp-grants --all

# 撤销单个授权（关联的 Access Token 和 Refresh Token 立即失效）
FLASK_APP=app.py flask mcp-revoke <grant_id>

# 撤销全部有效授权
FLASK_APP=app.py flask mcp-revoke --all
```

---

## 六、 协议与本地调试

- **传输**：无状态 Streamable HTTP（单 POST 返回 JSON，不开 SSE，无会话 ID；已认证的 `GET`/`DELETE` 返回 `405`）。
- **协议版本**：同时兼容 `2026-07-28`（无握手、`server/discover`、`Mcp-Method`/`Mcp-Name`/`MCP-Protocol-Version` 核对）和 `2025-11-25` / `2025-06-18` / `2025-03-26`（`initialize` 握手）。
- **限流**：按授权主体统计，每分钟最多 120 次工具调用、30 次写操作，超限返回温和提示。
- **调试工具**：可使用 `npx @modelcontextprotocol/inspector`，连接 `http://127.0.0.1:5100/mcp`（传输选 Streamable HTTP，请求头填 `Authorization: Bearer <MCP_ACCESS_TOKEN>`）。
