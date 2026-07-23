const DEFAULT_SERVER_URL = 'https://yter.cellmean.com';
const form = document.querySelector('#settings-form');
const serverUrlInput = document.querySelector('#server-url');
const status = document.querySelector('#status');

function normalizeServerUrl(value) {
  const parsed = new URL(value.trim());
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('服务地址必须使用 http 或 https');
  }
  parsed.hash = '';
  parsed.search = '';
  return parsed.href.replace(/\/+$/, '');
}

function permissionPattern(serverUrl) {
  const parsed = new URL(serverUrl);
  return `${parsed.protocol}//${parsed.host}/*`;
}

function setStatus(message, type) {
  status.textContent = message;
  status.className = type;
}

async function restoreSettings() {
  const stored = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER_URL });
  serverUrlInput.value = stored.serverUrl;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  setStatus('', '');

  let serverUrl;
  try {
    serverUrl = normalizeServerUrl(serverUrlInput.value);
  } catch (error) {
    setStatus(error.message, 'error');
    return;
  }

  try {
    const granted = await chrome.permissions.request({
      origins: [permissionPattern(serverUrl)],
    });
    if (!granted) {
      setStatus('未获得该站点的访问权限，设置未保存。', 'error');
      return;
    }

    await chrome.storage.sync.set({ serverUrl });
    serverUrlInput.value = serverUrl;
    setStatus('设置已保存，可以通过链接右键菜单提交下载。', 'success');
  } catch (error) {
    setStatus(`保存失败：${error.message}`, 'error');
  }
});

restoreSettings().catch((error) => {
  setStatus(`读取设置失败：${error.message}`, 'error');
});
