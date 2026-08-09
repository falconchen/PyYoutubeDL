const DEFAULT_SERVER_URL = 'https://yter.cellmean.com';
const form = document.querySelector('#settings-form');
const serverUrlInput = document.querySelector('#server-url');
const logTokenInput = document.querySelector('#log-token');
const status = document.querySelector('#status');
const openOptionsButton = document.querySelector('#open-options');

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
  const [synced, local] = await Promise.all([
    chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER_URL }),
    chrome.storage.local.get({ logToken: '' }),
  ]);
  serverUrlInput.value = synced.serverUrl;
  logTokenInput.value = local.logToken;
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

    await Promise.all([
      chrome.storage.sync.set({ serverUrl }),
      chrome.storage.local.set({ logToken: logTokenInput.value.trim() }),
    ]);
    serverUrlInput.value = serverUrl;
    setStatus('设置已保存，可以通过右键菜单提交下载。', 'success');
  } catch (error) {
    setStatus(`保存失败：${error.message}`, 'error');
  }
});

restoreSettings().catch((error) => {
  setStatus(`读取设置失败：${error.message}`, 'error');
});

if (openOptionsButton) {
  openOptionsButton.addEventListener('click', () => {
    chrome.runtime.openOptionsPage();
  });
}
