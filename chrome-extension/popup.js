const pageLabel = document.querySelector('#current-page-label');
const actionStatus = document.querySelector('#action-status');
const pageActionButtons = [...document.querySelectorAll('[data-page-action]')];
let activeTab = null;

function isSupportedPageUrl(value) {
  try {
    const parsed = new URL(value);
    return ['http:', 'https:'].includes(parsed.protocol);
  } catch (error) {
    return false;
  }
}

function pageDescription(tab) {
  const value = (tab.title || '').trim();
  if (value) return value;
  try {
    return new URL(tab.url).hostname;
  } catch (error) {
    return '当前标签页';
  }
}

function setActionStatus(message, type = '') {
  actionStatus.textContent = message;
  actionStatus.className = type;
}

function setActionsDisabled(disabled) {
  for (const button of pageActionButtons) {
    button.disabled = disabled;
  }
}

async function loadActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || typeof tab.id !== 'number' || !isSupportedPageUrl(tab.url)) {
    pageLabel.textContent = '此页面不支持下载或 AI 总结';
    setActionsDisabled(true);
    return;
  }
  activeTab = tab;
  pageLabel.textContent = pageDescription(tab);
  pageLabel.title = tab.url;
  setActionsDisabled(false);
}

async function runPageAction(button) {
  if (!activeTab) return;
  const action = button.dataset.pageAction;
  const actionLabel = {
    video: '视频下载',
    audio: '音频下载',
    'ai-summary': 'AI总结',
  }[action];
  setActionsDisabled(true);
  setActionStatus(`正在提交${actionLabel}…`);
  try {
    const response = await chrome.runtime.sendMessage({
      type: 'run-yter-page-action',
      action,
      tabId: activeTab.id,
      pageUrl: activeTab.url,
    });
    if (!response?.success) {
      throw new Error(response?.message || '后台未返回结果');
    }
    setActionStatus(response.message || `${actionLabel}已提交。`, 'success');
    if (action === 'ai-summary') window.close();
  } catch (error) {
    setActionStatus(`${actionLabel}失败：${error.message}`, 'error');
  } finally {
    setActionsDisabled(false);
  }
}

for (const button of pageActionButtons) {
  button.addEventListener('click', () => runPageAction(button));
}

loadActiveTab().catch((error) => {
  pageLabel.textContent = '无法读取当前标签页';
  setActionStatus(error.message, 'error');
  setActionsDisabled(true);
});
