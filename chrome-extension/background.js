const DEFAULT_SERVER_URL = 'https://yter.cellmean.com';
const MENU_PARENT = 'yter-download';
const MENU_VIDEO = 'yter-download-video';
const MENU_AUDIO = 'yter-download-audio';

function createContextMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_PARENT,
      title: '使用yter下载',
      contexts: ['link'],
      targetUrlPatterns: ['http://*/*', 'https://*/*'],
    });
    chrome.contextMenus.create({
      id: MENU_VIDEO,
      parentId: MENU_PARENT,
      title: '下载视频',
      contexts: ['link'],
      targetUrlPatterns: ['http://*/*', 'https://*/*'],
    });
    chrome.contextMenus.create({
      id: MENU_AUDIO,
      parentId: MENU_PARENT,
      title: '下载音频',
      contexts: ['link'],
      targetUrlPatterns: ['http://*/*', 'https://*/*'],
    });
  });
}

function normalizeServerUrl(value) {
  return (value || DEFAULT_SERVER_URL).trim().replace(/\/+$/, '');
}

async function readServerUrl() {
  const stored = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER_URL });
  return normalizeServerUrl(stored.serverUrl);
}

async function showNotification(title, message) {
  await chrome.notifications.create({
    type: 'basic',
    iconUrl: 'icons/icon128.png',
    title,
    message,
  });
}

async function addDownloadTask(linkUrl, type) {
  const serverUrl = await readServerUrl();
  const endpoint = `${serverUrl}/api/add_task`;

  let response;
  try {
    response = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        url: linkUrl,
        types: [type],
      }),
    });
  } catch (error) {
    throw new Error(`无法连接 yter：${error.message}`);
  }

  let result = {};
  try {
    result = await response.json();
  } catch (error) {
    // 非 JSON 错误页由下面的 HTTP 状态分支统一处理。
  }

  if (!response.ok || !result.success) {
    const detail = result.msg || `HTTP ${response.status}`;
    throw new Error(`yter 拒绝了任务：${detail}`);
  }

  return result.tasks || [];
}

chrome.runtime.onInstalled.addListener((details) => {
  createContextMenus();
  if (details.reason === 'install') {
    chrome.runtime.openOptionsPage();
  }
});

chrome.runtime.onStartup.addListener(createContextMenus);

chrome.action.onClicked.addListener(() => {
  chrome.runtime.openOptionsPage();
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  const typeByMenuId = {
    [MENU_VIDEO]: 'video',
    [MENU_AUDIO]: 'audio',
  };
  const type = typeByMenuId[info.menuItemId];
  if (!type || !info.linkUrl) {
    return;
  }

  const typeLabel = type === 'video' ? '视频' : '音频';
  try {
    const tasks = await addDownloadTask(info.linkUrl, type);
    const taskText = tasks.length ? `任务：${tasks.join(', ')}` : '任务已提交';
    await showNotification(`yter ${typeLabel}下载已加入队列`, taskText);
  } catch (error) {
    await showNotification(`yter ${typeLabel}下载失败`, error.message);
  }
});
