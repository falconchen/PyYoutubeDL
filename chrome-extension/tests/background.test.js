const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function createEvent() {
  return {
    listener: null,
    addListener(listener) {
      this.listener = listener;
    },
  };
}

function createHarness(harnessOptions = {}) {
  const localStorage = {};
  const alarms = new Map();
  const notifications = [];
  const overlayMessages = [];
  const injectedScripts = [];
  const addTaskRequests = [];
  const taskStates = new Map();
  let nextTaskNumber = 1;
  let pollError = null;

  const events = {
    alarm: createEvent(),
    contextClicked: createEvent(),
    installed: createEvent(),
    startup: createEvent(),
    actionClicked: createEvent(),
    runtimeMessage: createEvent(),
  };

  function storageArea(values) {
    return {
      async get(defaults) {
        return { ...defaults, ...values };
      },
      async set(items) {
        Object.assign(values, items);
      },
    };
  }

  const chrome = {
    action: {
      onClicked: events.actionClicked,
    },
    alarms: {
      onAlarm: events.alarm,
      async clear(name) {
        return alarms.delete(name);
      },
      async create(name, options) {
        alarms.set(name, { name, ...options });
      },
      async get(name) {
        return alarms.get(name);
      },
    },
    contextMenus: {
      onClicked: events.contextClicked,
      create() {},
      removeAll(callback) {
        callback();
      },
    },
    notifications: {
      async create(idOrOptions, maybeOptions) {
        const notification = maybeOptions
          ? { id: idOrOptions, ...maybeOptions }
          : { id: null, ...idOrOptions };
        notifications.push(notification);
        return notification.id || String(notifications.length);
      },
    },
    runtime: {
      onInstalled: events.installed,
      onMessage: events.runtimeMessage,
      onStartup: events.startup,
      openOptionsPage() {},
    },
    storage: {
      local: storageArea(localStorage),
      sync: storageArea({ serverUrl: 'https://yter.example' }),
    },
    scripting: {
      async executeScript(options) {
        injectedScripts.push(options);
      },
    },
    tabs: {
      async get(tabId) {
        return { id: tabId, url: 'https://video.example/watch/1' };
      },
      async query() {
        return [{ id: 7, url: 'https://video.example/watch/1', title: '测试视频' }];
      },
      async sendMessage(tabId, message) {
        overlayMessages.push({ tabId, message });
      },
    },
  };

  async function fetch(url, options) {
    if (url.endsWith('/api/add_task')) {
      const request = JSON.parse(options.body);
      addTaskRequests.push(request);
      const tasks = request.types.map((type) => {
        const prefix = type === 'video' ? 'v' : 'a';
        const task = `${prefix}2026072312000${nextTaskNumber}abc`;
        nextTaskNumber += 1;
        taskStates.set(task, { task, state: 'queued', type });
        return task;
      });
      return {
        ok: true,
        status: 200,
        async json() {
          return { success: true, tasks };
        },
      };
    }

    if (url.endsWith('/api/task_info')) {
      if (pollError) {
        throw pollError;
      }
      const tasks = JSON.parse(options.body).tasks;
      return {
        ok: true,
        status: 200,
        async json() {
          return {
            success: true,
            tasks: tasks.map((task) => taskStates.get(task)),
          };
        },
      };
    }

    if (url.endsWith('/api/ai_summaries')) {
      if (harnessOptions.streamAiSummary) {
        return {
          ok: true,
          status: 202,
          async json() {
            return { success: true, status: 'queued', job_id: 'summary-job-1' };
          },
        };
      }
      return {
        ok: true,
        status: 200,
        async json() {
          return {
            success: true,
            status: 'completed',
            cached: true,
            summary: { title: '测试视频', markdown: '# 已保存总结' },
          };
        },
      };
    }

    if (url.endsWith('/api/ai_summaries/jobs/summary-job-1/stream')) {
      const chunks = [
        '{"success":true,"status":"generating","job_id":"summary-job-1","partial_markdown":"## 部分"}\n',
        '{"success":true,"status":"completed","job_id":"summary-job-1","cached":false,"summary":{"title":"测试视频","markdown":"## 完整总结"}}\n',
      ].map((value) => new TextEncoder().encode(value));
      return {
        ok: true,
        status: 200,
        body: {
          getReader() {
            return {
              async read() {
                return chunks.length
                  ? { done: false, value: chunks.shift() }
                  : { done: true, value: undefined };
              },
            };
          },
        },
      };
    }

    throw new Error(`Unexpected URL: ${url}`);
  }

  const context = vm.createContext({
    chrome,
    console,
    fetch,
    TextDecoder,
    Uint8Array,
  });
  const source = fs.readFileSync(
    path.join(__dirname, '..', 'background.js'),
    'utf8',
  );
  vm.runInContext(source, context);

  return {
    addTaskRequests,
    alarms,
    events,
    localStorage,
    notifications,
    overlayMessages,
    injectedScripts,
    taskStates,
    setPollError(error) {
      pollError = error;
    },
  };
}

async function submitVideoTask(harness) {
  await harness.events.contextClicked.listener({
    menuItemId: 'yter-download-video',
    pageUrl: 'https://video.example/watch/1',
  });
  return Object.keys(harness.localStorage.pendingTasks)[0];
}

test('tracks a submitted task and notifies once when it completes', async () => {
  const harness = createHarness();
  const task = await submitVideoTask(harness);

  assert.equal(harness.localStorage.pendingTasks[task].type, 'video');
  assert.equal(
    harness.alarms.get('yter-task-poll').periodInMinutes,
    0.5,
  );

  harness.taskStates.set(task, {
    task,
    state: 'completed',
    files: ['finished-video.mp4'],
  });
  await harness.events.alarm.listener({ name: 'yter-task-poll' });

  assert.equal(Object.keys(harness.localStorage.pendingTasks).length, 0);
  assert.equal(harness.alarms.has('yter-task-poll'), false);
  assert.equal(
    harness.notifications.filter(
      (notification) => notification.id === `yter-completed-${task}`,
    ).length,
    1,
  );
  assert.match(harness.notifications.at(-1).title, /视频下载完成/);
  assert.equal(harness.notifications.at(-1).message, 'finished-video.mp4');
});

test('keeps a task after a polling network error', async () => {
  const harness = createHarness();
  const task = await submitVideoTask(harness);
  harness.setPollError(new Error('offline'));

  await harness.events.alarm.listener({ name: 'yter-task-poll' });

  assert.ok(harness.localStorage.pendingTasks[task]);
  assert.equal(harness.alarms.has('yter-task-poll'), true);
});

test('stops tracking after three missing responses', async () => {
  const harness = createHarness();
  const task = await submitVideoTask(harness);
  harness.taskStates.set(task, { task, state: 'missing' });

  await harness.events.alarm.listener({ name: 'yter-task-poll' });
  await harness.events.alarm.listener({ name: 'yter-task-poll' });
  assert.ok(harness.localStorage.pendingTasks[task]);

  await harness.events.alarm.listener({ name: 'yter-task-poll' });
  assert.equal(Object.keys(harness.localStorage.pendingTasks).length, 0);
  assert.equal(
    harness.notifications.at(-1).id,
    `yter-missing-${task}`,
  );
});

test('toolbar action opens the inline settings popup', () => {
  const extensionRoot = path.join(__dirname, '..');
  const manifest = JSON.parse(
    fs.readFileSync(path.join(extensionRoot, 'manifest.json'), 'utf8'),
  );
  const popup = fs.readFileSync(
    path.join(extensionRoot, 'popup.html'),
    'utf8',
  );
  const popupStyles = fs.readFileSync(
    path.join(extensionRoot, 'popup.css'),
    'utf8',
  );
  const background = fs.readFileSync(
    path.join(extensionRoot, 'background.js'),
    'utf8',
  );

  assert.equal(manifest.version, '1.5.2');
  assert.equal(manifest.action.default_popup, 'popup.html');
  assert.match(popup, /<details class="settings-card">/);
  assert.doesNotMatch(popup, /<details class="settings-card" open>/);
  assert.match(popup, /id="settings-form"/);
  assert.match(popup, /id="server-url"/);
  assert.match(popup, /id="status"/);
  assert.match(popup, /id="ai-summary-token"/);
  assert.match(popup, /data-page-action="video"/);
  assert.match(popup, /data-page-action="audio"/);
  assert.match(popup, /data-page-action="video-audio"/);
  assert.match(popup, /视频\+音频/);
  assert.doesNotMatch(popup, /data-page-action="ai-summary"/);
  assert.match(background, /id: MENU_VIDEO_AUDIO,[\s\S]*?title: '视频\+音频'/);
  assert.match(popup, /src="options\.js"/);
  assert.match(popup, /src="popup\.js"/);
  assert.match(popupStyles, /html,\s*body\s*\{[^}]*width:\s*360px;/s);
  assert.match(
    popupStyles,
    /\.popup-shell\s*\{[^}]*max-height:\s*600px;[^}]*overflow-x:\s*hidden;[^}]*overflow-y:\s*auto;/s,
  );
});

test('toolbar page action submits the active page as a video task', async () => {
  const harness = createHarness();
  const response = await new Promise((resolve) => {
    const keepChannelOpen = harness.events.runtimeMessage.listener({
      type: 'run-yter-page-action',
      action: 'video',
      tabId: 7,
      pageUrl: 'https://video.example/watch/1',
    }, {}, resolve);
    assert.equal(keepChannelOpen, true);
  });

  assert.equal(response.success, true);
  const [task] = Object.keys(harness.localStorage.pendingTasks);
  assert.equal(
    harness.localStorage.pendingTasks[task].sourceUrl,
    'https://video.example/watch/1',
  );
  assert.match(harness.notifications.at(-1).title, /视频下载已加入队列/);
});

test('toolbar combined action submits video and audio in one request', async () => {
  const harness = createHarness();
  const response = await new Promise((resolve) => {
    const keepChannelOpen = harness.events.runtimeMessage.listener({
      type: 'run-yter-page-action',
      action: 'video-audio',
      tabId: 7,
      pageUrl: 'https://video.example/watch/1',
    }, {}, resolve);
    assert.equal(keepChannelOpen, true);
  });

  assert.equal(response.success, true);
  assert.equal(harness.addTaskRequests.length, 1);
  assert.deepEqual(harness.addTaskRequests[0].types, ['video', 'audio']);
  assert.equal(Object.keys(harness.localStorage.pendingTasks).length, 2);
});

test('combined context-menu action submits video and audio in one request', async () => {
  const harness = createHarness();

  await harness.events.contextClicked.listener({
    menuItemId: 'yter-download-video-audio',
    pageUrl: 'https://video.example/watch/1',
  });

  assert.equal(harness.addTaskRequests.length, 1);
  assert.deepEqual(harness.addTaskRequests[0].types, ['video', 'audio']);
  const pendingTypes = Object.values(harness.localStorage.pendingTasks)
    .map((task) => task.type);
  assert.deepEqual(pendingTypes, ['video', 'audio']);
  assert.match(harness.notifications.at(-1).title, /视频\+音频下载已加入队列/);
});

async function runAiSummaryCompatibilityAction(harness) {
  const response = await new Promise((resolve) => {
    const keepChannelOpen = harness.events.runtimeMessage.listener({
      type: 'run-yter-page-action',
      action: 'ai-summary',
      tabId: 7,
      pageUrl: 'https://video.example/watch/1',
    }, {}, resolve);
    assert.equal(keepChannelOpen, true);
  });
  assert.equal(response.success, true);
  await new Promise((resolve) => setImmediate(resolve));
}

test('legacy AI summary runtime action still returns cached content', async () => {
  const harness = createHarness();
  harness.localStorage.aiSummaryToken = 'ai-token';

  await runAiSummaryCompatibilityAction(harness);

  assert.equal(harness.injectedScripts.length, 1);
  assert.equal(harness.injectedScripts[0].files[0], 'summary-overlay.bundle.js');
  assert.equal(harness.overlayMessages.at(-1).message.type, 'completed');
  assert.equal(harness.overlayMessages.at(-1).message.summary.markdown, '# 已保存总结');
  const source = fs.readFileSync(
    path.join(__dirname, '..', 'summary-overlay-entry.js'),
    'utf8',
  );
  assert.match(source, /DOMPurify\.sanitize/);
  assert.match(source, /FORBID_TAGS/);
  assert.doesNotMatch(source, /content\.classList\.add\('collapsed'\)/);
  assert.match(source, /toggle\.textContent = '收起'/);
  assert.match(source, /toggle\.setAttribute\('aria-expanded', 'true'\)/);
});

test('legacy AI summary runtime action forwards streamed markdown and final result', async () => {
  const harness = createHarness({ streamAiSummary: true });
  harness.localStorage.aiSummaryToken = 'extension-ai-token';

  await runAiSummaryCompatibilityAction(harness);

  const messages = harness.overlayMessages.map((entry) => entry.message);
  assert.ok(messages.some(
    (message) => message.type === 'streaming' && message.markdown === '## 部分',
  ), JSON.stringify(messages));
  assert.ok(messages.some(
    (message) => message.type === 'completed'
      && message.summary.markdown === '## 完整总结',
  ));
  assert.equal(Object.keys(harness.localStorage.pendingAiSummaries || {}).length, 0);
});

test('extension includes a token-protected live log page', () => {
  const extensionRoot = path.join(__dirname, '..');
  const popup = fs.readFileSync(
    path.join(extensionRoot, 'popup.html'),
    'utf8',
  );
  const logPage = fs.readFileSync(
    path.join(extensionRoot, 'logs.html'),
    'utf8',
  );
  const logScript = fs.readFileSync(
    path.join(extensionRoot, 'logs.js'),
    'utf8',
  );

  assert.match(popup, /href="logs\.html"/);
  assert.match(popup, /id="log-token"/);
  assert.match(logPage, /id="log-output"/);
  assert.match(logPage, /id="pause-button"/);
  assert.match(logScript, /\/api\/downloader_log/);
  assert.match(logScript, /X-Yter-Log-Token/);
  assert.match(logScript, /row\.textContent = line/);
});
