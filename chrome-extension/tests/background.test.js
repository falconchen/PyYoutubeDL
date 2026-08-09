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

function createHarness() {
  const localStorage = {};
  const alarms = new Map();
  const notifications = [];
  const taskStates = new Map();
  let nextTaskNumber = 1;
  let pollError = null;

  const events = {
    alarm: createEvent(),
    contextClicked: createEvent(),
    installed: createEvent(),
    startup: createEvent(),
    actionClicked: createEvent(),
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
      onStartup: events.startup,
      openOptionsPage() {},
    },
    storage: {
      local: storageArea(localStorage),
      sync: storageArea({ serverUrl: 'https://yter.example' }),
    },
  };

  async function fetch(url, options) {
    if (url.endsWith('/api/add_task')) {
      const task = `v2026072312000${nextTaskNumber}abc`;
      nextTaskNumber += 1;
      taskStates.set(task, {
        task,
        state: 'queued',
        type: JSON.parse(options.body).types[0],
      });
      return {
        ok: true,
        status: 200,
        async json() {
          return { success: true, tasks: [task] };
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

    throw new Error(`Unexpected URL: ${url}`);
  }

  const context = vm.createContext({
    chrome,
    console,
    fetch,
  });
  const source = fs.readFileSync(
    path.join(__dirname, '..', 'background.js'),
    'utf8',
  );
  vm.runInContext(source, context);

  return {
    alarms,
    events,
    localStorage,
    notifications,
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

  assert.equal(manifest.version, '1.3.0');
  assert.equal(manifest.action.default_popup, 'popup.html');
  assert.match(popup, /id="settings-form"/);
  assert.match(popup, /id="server-url"/);
  assert.match(popup, /id="status"/);
  assert.match(popup, /src="options\.js"/);
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
