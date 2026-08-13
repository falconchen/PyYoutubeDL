const DEFAULT_SERVER_URL = 'https://yter.cellmean.com';
const MENU_PARENT = 'yter-download';
const MENU_VIDEO = 'yter-download-video';
const MENU_AUDIO = 'yter-download-audio';
const MENU_AI_SUMMARY = 'yter-ai-summary';
const PENDING_TASKS_KEY = 'pendingTasks';
const PENDING_SUMMARIES_KEY = 'pendingAiSummaries';
const TASK_POLL_ALARM = 'yter-task-poll';
const SUMMARY_POLL_ALARM = 'yter-ai-summary-poll';
const TASK_POLL_INTERVAL_MINUTES = 0.5;
const MISSING_POLL_LIMIT = 3;

let pendingTasksUpdate = Promise.resolve();
let activePoll = null;

function createContextMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_PARENT,
      title: '使用yter下载',
      contexts: ['link', 'page'],
      documentUrlPatterns: ['http://*/*', 'https://*/*'],
    });
    chrome.contextMenus.create({
      id: MENU_VIDEO,
      parentId: MENU_PARENT,
      title: '下载视频',
      contexts: ['link', 'page'],
      documentUrlPatterns: ['http://*/*', 'https://*/*'],
    });
    chrome.contextMenus.create({
      id: MENU_AUDIO,
      parentId: MENU_PARENT,
      title: '下载音频',
      contexts: ['link', 'page'],
      documentUrlPatterns: ['http://*/*', 'https://*/*'],
    });
    chrome.contextMenus.create({
      id: MENU_AI_SUMMARY,
      parentId: MENU_PARENT,
      title: 'AI总结',
      contexts: ['link', 'page'],
      documentUrlPatterns: ['http://*/*', 'https://*/*'],
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

async function readAiSummaryToken() {
  const stored = await chrome.storage.local.get({ aiSummaryToken: '' });
  return (stored.aiSummaryToken || '').trim();
}

async function sendSummaryOverlayMessage(tabId, message) {
  await chrome.tabs.sendMessage(tabId, {
    channel: 'yter-ai-summary',
    ...message,
  });
}

async function injectSummaryOverlay(tabId) {
  await chrome.scripting.executeScript({
    target: { tabId },
    files: ['summary-overlay.bundle.js'],
  });
}

async function fetchAiSummaryJob(serverUrl, token, jobId) {
  const response = await fetch(`${serverUrl}/api/ai_summaries/jobs/${jobId}`, {
    headers: { 'X-Yter-AI-Token': token },
  });
  let result = {};
  try {
    result = await response.json();
  } catch (error) {
    // 非 JSON 错误页由状态分支处理。
  }
  if (![200, 202, 422].includes(response.status)) {
    throw new Error(result.message || `HTTP ${response.status}`);
  }
  return { response, result };
}

async function submitAiSummary(serverUrl, token, sourceUrl) {
  const response = await fetch(`${serverUrl}/api/ai_summaries`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Yter-AI-Token': token,
    },
    body: JSON.stringify({ url: sourceUrl }),
  });
  let result = {};
  try {
    result = await response.json();
  } catch (error) {
    // 非 JSON 错误页由状态分支处理。
  }
  if (![200, 202].includes(response.status) || !result.success) {
    throw new Error(result.message || `HTTP ${response.status}`);
  }
  return { response, result };
}

async function getPendingSummaries() {
  const stored = await chrome.storage.local.get({ [PENDING_SUMMARIES_KEY]: {} });
  return stored[PENDING_SUMMARIES_KEY] || {};
}

async function savePendingSummaries(pending) {
  await chrome.storage.local.set({ [PENDING_SUMMARIES_KEY]: pending });
  if (Object.keys(pending).length) {
    const alarm = await chrome.alarms.get(SUMMARY_POLL_ALARM);
    if (!alarm) {
      await chrome.alarms.create(SUMMARY_POLL_ALARM, {
        periodInMinutes: TASK_POLL_INTERVAL_MINUTES,
      });
    }
  } else {
    await chrome.alarms.clear(SUMMARY_POLL_ALARM);
  }
}

async function ensureSummaryPollingAlarm() {
  await savePendingSummaries(await getPendingSummaries());
}

async function trackAiSummary(jobId, tabId, pageUrl, serverUrl) {
  const pending = await getPendingSummaries();
  pending[jobId] = { jobId, tabId, pageUrl, serverUrl, submittedAt: Date.now() };
  await savePendingSummaries(pending);
}

const SUMMARY_STAGE_TEXT = {
  queued: '总结任务已排队…',
  resolving: '正在读取媒体信息…',
  downloading_subtitle: '正在下载字幕…',
  generating: '正在调用 AI 生成总结…',
};

async function deliverAiSummaryResult(pendingItem, result) {
  if (result.status === 'completed' && result.summary) {
    try {
      const tab = await chrome.tabs.get(pendingItem.tabId);
      if (!tab || tab.url !== pendingItem.pageUrl) throw new Error('页面已经离开');
      await sendSummaryOverlayMessage(pendingItem.tabId, {
        type: 'completed',
        summary: result.summary,
        cached: Boolean(result.cached),
      });
    } catch (error) {
      await showNotification('yter AI总结已完成', result.summary.title || pendingItem.pageUrl);
    }
    return true;
  }
  if (result.status === 'failed') {
    const message = result.error?.message || '生成总结失败';
    try {
      await sendSummaryOverlayMessage(pendingItem.tabId, { type: 'error', text: message });
    } catch (error) {
      await showNotification('yter AI总结失败', message);
    }
    return true;
  }
  try {
    await sendSummaryOverlayMessage(pendingItem.tabId, {
      type: 'pending',
      text: SUMMARY_STAGE_TEXT[result.status] || '正在生成总结…',
      jobId: pendingItem.jobId,
    });
  } catch (error) {
    // alarm 后续仍会继续跟踪。
  }
  return false;
}

async function pollAiSummary(jobId) {
  const pending = await getPendingSummaries();
  const item = pending[jobId];
  if (!item) return { success: false, message: '任务不再跟踪' };
  const token = await readAiSummaryToken();
  if (!token) throw new Error('请先配置 AI总结访问令牌');
  const { result } = await fetchAiSummaryJob(item.serverUrl, token, jobId);
  if (await deliverAiSummaryResult(item, result)) {
    delete pending[jobId];
    await savePendingSummaries(pending);
  }
  return result;
}

async function pollAllAiSummaries() {
  const pending = await getPendingSummaries();
  for (const jobId of Object.keys(pending)) {
    try {
      await pollAiSummary(jobId);
    } catch (error) {
      console.warn(`无法轮询 AI 总结任务 ${jobId}`, error);
    }
  }
}

async function handleAiSummaryClick(targetUrl, tab) {
  if (!tab || typeof tab.id !== 'number') return;
  await injectSummaryOverlay(tab.id);
  await sendSummaryOverlayMessage(tab.id, { type: 'pending', text: '正在查询已保存的总结…' });
  const [serverUrl, token] = await Promise.all([readServerUrl(), readAiSummaryToken()]);
  if (!token) {
    await sendSummaryOverlayMessage(tab.id, {
      type: 'error',
      text: '请先在扩展设置中填写 AI总结访问令牌。',
    });
    return;
  }
  try {
    const { result } = await submitAiSummary(serverUrl, token, targetUrl);
    if (result.status === 'completed') {
      await sendSummaryOverlayMessage(tab.id, {
        type: 'completed',
        summary: result.summary,
        cached: Boolean(result.cached),
      });
      return;
    }
    await trackAiSummary(result.job_id, tab.id, tab.url, serverUrl);
    await sendSummaryOverlayMessage(tab.id, {
      type: 'pending',
      text: SUMMARY_STAGE_TEXT[result.status] || '总结任务已提交…',
      jobId: result.job_id,
    });
  } catch (error) {
    await sendSummaryOverlayMessage(tab.id, { type: 'error', text: error.message });
  }
}

async function showNotification(title, message, notificationId) {
  const options = {
    type: 'basic',
    iconUrl: 'icons/icon128.png',
    title,
    message,
  };
  if (notificationId) {
    await chrome.notifications.create(notificationId, options);
    return;
  }
  await chrome.notifications.create(options);
}

async function getPendingTasks() {
  const stored = await chrome.storage.local.get({ [PENDING_TASKS_KEY]: {} });
  return stored[PENDING_TASKS_KEY] || {};
}

function updatePendingTasks(mutator) {
  const operation = pendingTasksUpdate.then(async () => {
    const pendingTasks = await getPendingTasks();
    await mutator(pendingTasks);
    await chrome.storage.local.set({ [PENDING_TASKS_KEY]: pendingTasks });
    return pendingTasks;
  });
  pendingTasksUpdate = operation.catch(() => {});
  return operation;
}

async function ensureTaskPollingAlarm() {
  await pendingTasksUpdate;
  const pendingTasks = await getPendingTasks();
  if (Object.keys(pendingTasks).length === 0) {
    await chrome.alarms.clear(TASK_POLL_ALARM);
    return;
  }

  const alarm = await chrome.alarms.get(TASK_POLL_ALARM);
  if (!alarm) {
    await chrome.alarms.create(TASK_POLL_ALARM, {
      periodInMinutes: TASK_POLL_INTERVAL_MINUTES,
    });
  }
}

async function trackPendingTasks(tasks, type, serverUrl, sourceUrl) {
  const validTasks = tasks.filter((task) => typeof task === 'string' && task);
  if (validTasks.length === 0) {
    return;
  }

  await updatePendingTasks((pendingTasks) => {
    for (const task of validTasks) {
      pendingTasks[task] = {
        task,
        type,
        serverUrl,
        sourceUrl,
        submittedAt: Date.now(),
        missingPolls: 0,
      };
    }
  });
  await ensureTaskPollingAlarm();
}

async function fetchTaskInfo(serverUrl, tasks) {
  const response = await fetch(`${serverUrl}/api/task_info`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ tasks }),
  });

  let result = {};
  try {
    result = await response.json();
  } catch (error) {
    // 非 JSON 错误页由下面的状态检查统一处理。
  }
  if (!response.ok || !result.success || !Array.isArray(result.tasks)) {
    const detail = result.msg || `HTTP ${response.status}`;
    throw new Error(`查询任务状态失败：${detail}`);
  }
  return result.tasks;
}

function completionMessage(taskInfo) {
  if (Array.isArray(taskInfo.files) && taskInfo.files.length > 0) {
    return taskInfo.files.join(', ');
  }
  return `任务：${taskInfo.task}`;
}

async function pollPendingTasksOnce() {
  await pendingTasksUpdate;
  const pendingTasks = await getPendingTasks();
  const groupedTasks = {};

  for (const pendingTask of Object.values(pendingTasks)) {
    if (!groupedTasks[pendingTask.serverUrl]) {
      groupedTasks[pendingTask.serverUrl] = [];
    }
    groupedTasks[pendingTask.serverUrl].push(pendingTask.task);
  }

  for (const [serverUrl, tasks] of Object.entries(groupedTasks)) {
    let taskResults;
    try {
      taskResults = await fetchTaskInfo(serverUrl, tasks);
    } catch (error) {
      console.warn(`无法轮询 ${serverUrl} 的 yter 任务`, error);
      continue;
    }

    const notifications = [];
    await updatePendingTasks((currentTasks) => {
      for (const taskInfo of taskResults) {
        const pendingTask = currentTasks[taskInfo.task];
        if (!pendingTask) {
          continue;
        }

        const typeLabel = pendingTask.type === 'video' ? '视频' : '音频';
        if (taskInfo.state === 'completed') {
          delete currentTasks[taskInfo.task];
          notifications.push({
            id: `yter-completed-${taskInfo.task}`,
            title: `yter ${typeLabel}下载完成`,
            message: completionMessage(taskInfo),
          });
        } else if (taskInfo.state === 'failed') {
          delete currentTasks[taskInfo.task];
          notifications.push({
            id: `yter-failed-${taskInfo.task}`,
            title: `yter ${typeLabel}下载失败`,
            message: taskInfo.msg || `任务：${taskInfo.task}`,
          });
        } else if (taskInfo.state === 'missing') {
          pendingTask.missingPolls = (pendingTask.missingPolls || 0) + 1;
          if (pendingTask.missingPolls >= MISSING_POLL_LIMIT) {
            delete currentTasks[taskInfo.task];
            notifications.push({
              id: `yter-missing-${taskInfo.task}`,
              title: `yter ${typeLabel}任务无法继续跟踪`,
              message: `连续 ${MISSING_POLL_LIMIT} 次未找到任务：${taskInfo.task}`,
            });
          }
        } else {
          pendingTask.missingPolls = 0;
        }
      }
    });

    for (const notification of notifications) {
      await showNotification(
        notification.title,
        notification.message,
        notification.id,
      );
    }
  }

  await ensureTaskPollingAlarm();
}

function pollPendingTasks() {
  if (!activePoll) {
    activePoll = pollPendingTasksOnce().finally(() => {
      activePoll = null;
    });
  }
  return activePoll;
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

  return {
    tasks: result.tasks || [],
    serverUrl,
  };
}

chrome.runtime.onInstalled.addListener((details) => {
  createContextMenus();
  ensureTaskPollingAlarm().catch((error) => {
    console.warn('恢复 yter 任务轮询失败', error);
  });
  ensureSummaryPollingAlarm().catch((error) => {
    console.warn('恢复 AI 总结任务轮询失败', error);
  });
  if (details.reason === 'install') {
    chrome.runtime.openOptionsPage();
  }
});

chrome.runtime.onStartup.addListener(() => {
  createContextMenus();
  ensureTaskPollingAlarm().catch((error) => {
    console.warn('恢复 yter 任务轮询失败', error);
  });
  ensureSummaryPollingAlarm().catch((error) => {
    console.warn('恢复 AI 总结任务轮询失败', error);
  });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type !== 'poll-yter-ai-summary' || !message.jobId) return undefined;
  pollAiSummary(message.jobId)
    .then((result) => sendResponse({ success: true, result }))
    .catch((error) => sendResponse({ success: false, message: error.message }));
  return true;
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === TASK_POLL_ALARM) {
    return pollPendingTasks().catch((error) => {
      console.warn('轮询 yter 任务失败', error);
    });
  }
  if (alarm.name === SUMMARY_POLL_ALARM) {
    return pollAllAiSummaries();
  }
  return undefined;
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const targetUrl = info.linkUrl || info.pageUrl;
  if (info.menuItemId === MENU_AI_SUMMARY && targetUrl) {
    return handleAiSummaryClick(targetUrl, tab).catch(async (error) => {
      await showNotification('yter AI总结失败', error.message);
    });
  }
  const typeByMenuId = {
    [MENU_VIDEO]: 'video',
    [MENU_AUDIO]: 'audio',
  };
  const type = typeByMenuId[info.menuItemId];
  if (!type || !targetUrl) {
    return;
  }

  const typeLabel = type === 'video' ? '视频' : '音频';
  let result;
  try {
    result = await addDownloadTask(targetUrl, type);
  } catch (error) {
    await showNotification(`yter ${typeLabel}下载失败`, error.message);
    return;
  }

  try {
    await trackPendingTasks(result.tasks, type, result.serverUrl, targetUrl);
  } catch (error) {
    await showNotification(
      `yter ${typeLabel}任务已提交，但无法跟踪`,
      error.message,
    );
    return;
  }

  const taskText = result.tasks.length
    ? `任务：${result.tasks.join(', ')}`
    : '任务已提交';
  await showNotification(`yter ${typeLabel}下载已加入队列`, taskText);
});
