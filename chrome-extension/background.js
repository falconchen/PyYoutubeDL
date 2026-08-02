const DEFAULT_SERVER_URL = 'https://yter.cellmean.com';
const MENU_PARENT = 'yter-download';
const MENU_VIDEO = 'yter-download-video';
const MENU_AUDIO = 'yter-download-audio';
const PENDING_TASKS_KEY = 'pendingTasks';
const TASK_POLL_ALARM = 'yter-task-poll';
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
  });
}

function normalizeServerUrl(value) {
  return (value || DEFAULT_SERVER_URL).trim().replace(/\/+$/, '');
}

async function readServerUrl() {
  const stored = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER_URL });
  return normalizeServerUrl(stored.serverUrl);
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
  if (details.reason === 'install') {
    chrome.runtime.openOptionsPage();
  }
});

chrome.runtime.onStartup.addListener(() => {
  createContextMenus();
  ensureTaskPollingAlarm().catch((error) => {
    console.warn('恢复 yter 任务轮询失败', error);
  });
});

chrome.action.onClicked.addListener(() => {
  chrome.runtime.openOptionsPage();
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === TASK_POLL_ALARM) {
    return pollPendingTasks().catch((error) => {
      console.warn('轮询 yter 任务失败', error);
    });
  }
  return undefined;
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  const typeByMenuId = {
    [MENU_VIDEO]: 'video',
    [MENU_AUDIO]: 'audio',
  };
  const type = typeByMenuId[info.menuItemId];
  const targetUrl = info.linkUrl || info.pageUrl;
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
