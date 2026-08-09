const DEFAULT_SERVER_URL = 'https://yter.cellmean.com';
const POLL_INTERVAL_MS = 1000;
const ERROR_RETRY_MS = 3000;
const MAX_RENDERED_LINES = 3000;

const logOutput = document.querySelector('#log-output');
const emptyMessage = document.querySelector('#empty-message');
const serverLabel = document.querySelector('#server-label');
const connectionDot = document.querySelector('#connection-dot');
const connectionStatus = document.querySelector('#connection-status');
const pauseButton = document.querySelector('#pause-button');
const clearButton = document.querySelector('#clear-button');
const reconnectButton = document.querySelector('#reconnect-button');
const autoscrollButton = document.querySelector('#autoscroll-button');

let serverUrl = DEFAULT_SERVER_URL;
let logToken = '';
let cursor = null;
let fileId = null;
let pollTimer = null;
let pollInFlight = false;
let running = true;
let autoScroll = true;

function normalizeServerUrl(value) {
  return (value || DEFAULT_SERVER_URL).trim().replace(/\/+$/, '');
}

function setConnectionState(state, message) {
  connectionDot.className = `connection-dot ${state}`;
  connectionStatus.textContent = message;
}

function lineClass(line) {
  if (line.includes('[ERROR]')) return 'error';
  if (line.includes('[WARNING]')) return 'warning';
  if (line.includes('PYDL_PROGRESS|') || line.includes('[download]')) {
    return 'progress';
  }
  return 'info';
}

function isNearBottom() {
  return logOutput.scrollHeight - logOutput.scrollTop - logOutput.clientHeight < 48;
}

function updateAutoscrollButton() {
  autoscrollButton.classList.toggle('active', autoScroll);
  autoscrollButton.setAttribute('aria-pressed', String(autoScroll));
  autoscrollButton.textContent = `自动滚动：${autoScroll ? '开' : '关'}`;
}

function appendLogText(text) {
  if (!text) return;
  const stickToBottom = autoScroll || isNearBottom();
  emptyMessage.hidden = true;
  const fragment = document.createDocumentFragment();

  for (const line of text.split(/\r?\n/)) {
    if (!line) continue;
    const row = document.createElement('div');
    row.className = `log-line ${lineClass(line)}`;
    row.textContent = line;
    fragment.appendChild(row);
  }
  logOutput.appendChild(fragment);

  const rows = logOutput.querySelectorAll('.log-line');
  const excess = rows.length - MAX_RENDERED_LINES;
  for (let index = 0; index < excess; index += 1) {
    rows[index].remove();
  }

  if (stickToBottom) {
    logOutput.scrollTop = logOutput.scrollHeight;
  }
}

function clearPollTimer() {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function schedulePoll(delay) {
  clearPollTimer();
  if (running && !document.hidden) {
    pollTimer = setTimeout(pollLog, delay);
  }
}

async function readSettings() {
  const [synced, local] = await Promise.all([
    chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER_URL }),
    chrome.storage.local.get({ logToken: '' }),
  ]);
  serverUrl = normalizeServerUrl(synced.serverUrl);
  logToken = local.logToken.trim();
  serverLabel.textContent = serverUrl;
  if (!logToken) {
    throw new Error('请先在扩展设置中填写日志访问令牌');
  }
}

async function pollLog() {
  if (!running || pollInFlight || document.hidden) return;
  pollInFlight = true;
  setConnectionState('connecting', '正在读取');

  try {
    const queryParams = new URLSearchParams();
    if (cursor !== null) queryParams.set('cursor', cursor);
    if (fileId) queryParams.set('file_id', fileId);
    const query = queryParams.size ? `?${queryParams.toString()}` : '';
    const response = await fetch(`${serverUrl}/api/downloader_log${query}`, {
      cache: 'no-store',
      headers: {
        'X-Yter-Log-Token': logToken,
      },
    });
    let result = {};
    try {
      result = await response.json();
    } catch (error) {
      // 非 JSON 错误页由下面的状态检查处理。
    }
    if (!response.ok || !result.success) {
      if (response.status === 401) {
        throw new Error('日志访问令牌错误');
      }
      if (response.status === 503) {
        throw new Error('服务器尚未配置 EXTENSION_LOG_TOKEN');
      }
      throw new Error(result.msg || `HTTP ${response.status}`);
    }

    if (result.reset) {
      logOutput.querySelectorAll('.log-line').forEach((row) => row.remove());
      emptyMessage.hidden = false;
      emptyMessage.textContent = '日志已轮转，继续读取新文件…';
    }
    appendLogText(result.text);
    cursor = result.cursor;
    fileId = result.file_id;
    setConnectionState('connected', '实时连接中');
    schedulePoll(result.has_more ? 0 : POLL_INTERVAL_MS);
  } catch (error) {
    setConnectionState('error', error.message);
    schedulePoll(ERROR_RETRY_MS);
  } finally {
    pollInFlight = false;
  }
}

function setRunning(value) {
  running = value;
  pauseButton.innerHTML = value
    ? '<span aria-hidden="true">Ⅱ</span> 暂停'
    : '<span aria-hidden="true">▶</span> 继续';
  if (value) {
    schedulePoll(0);
  } else {
    clearPollTimer();
    setConnectionState('connecting', '已暂停');
  }
}

pauseButton.addEventListener('click', () => setRunning(!running));

clearButton.addEventListener('click', () => {
  logOutput.querySelectorAll('.log-line').forEach((row) => row.remove());
  emptyMessage.hidden = false;
  emptyMessage.textContent = '显示已清空，等待新日志…';
});

reconnectButton.addEventListener('click', async () => {
  cursor = null;
  fileId = null;
  clearPollTimer();
  try {
    await readSettings();
    setRunning(true);
  } catch (error) {
    setConnectionState('error', error.message);
  }
});

autoscrollButton.addEventListener('click', () => {
  autoScroll = !autoScroll;
  updateAutoscrollButton();
  if (autoScroll) {
    logOutput.scrollTop = logOutput.scrollHeight;
  }
});

logOutput.addEventListener('scroll', () => {
  if (autoScroll && !isNearBottom()) {
    autoScroll = false;
    updateAutoscrollButton();
  }
});

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    clearPollTimer();
  } else if (running) {
    schedulePoll(0);
  }
});

readSettings()
  .then(() => schedulePoll(0))
  .catch((error) => {
    setConnectionState('error', error.message);
    emptyMessage.textContent = '请打开扩展设置，填写与服务器一致的日志访问令牌。';
  });
