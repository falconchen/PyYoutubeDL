/* DropLoad 下载页交互：任务入队、进度轮询与任务日志抽屉。 */
(function () {
    'use strict';

    var TASKS_KEY = 'dropload.tasks';
    var TYPES_KEY = 'selectedTypes';
    // /api/task_log 最多接受 20 个任务，本地列表保持同一上限。
    var MAX_TASKS = 20;
    var TASK_POLL_MS = 2000;
    var LOG_POLL_MS = 1500;
    var TASK_ID_PATTERN = /^[va][A-Za-z0-9_-]{1,127}$/;
    var METADATA_TIMEOUT_MS = 30000;
    var METADATA_MAX_ATTEMPTS = 2;
    var METADATA_RETRY_DELAY_MS = 1500;
    var PROGRESS_KEY_PREFIX = 'dropload:playback-progress:';
    var PLAYBACK_RATE_KEY = 'dropload:playback-rate';
    // 距结尾不足这么多秒视为播完，不再记忆进度。
    var PLAYBACK_END_THRESHOLD = 3;
    var PROGRESS_SAVE_INTERVAL_MS = 5000;
    // 与 zwplayer 倍速菜单的档位保持一致，其余值不记忆。
    var PLAYBACK_RATES = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2];
    var SEEK_OFFSET_SECONDS = 10;
    var LRC_TIMESTAMP = /\[\d{1,2}:\d{1,2}/;
    // zwplayer 字幕设置面板里「字幕大小：较小」对应的值（默认「适中」为 0.2）
    var SUBTITLE_FONT_SIZE_SMALL = '0.14';
    // 字幕基准字号（「适中」档）按播放器尺寸等比：取宽度 4.2%、高度 7.5% 中较小者，
    // 页面内约 760×428 时为 32px，与 zwplayer 原算法一致；实际字号最大 36px，免得全屏时过大。
    var SUBTITLE_BASE_WIDTH_RATIO = 0.042;
    var SUBTITLE_BASE_HEIGHT_RATIO = 0.075;
    var SUBTITLE_BASE_MIN_PX = 14;
    var SUBTITLE_MAX_PX = 36;
    var SUBTITLE_MEDIUM_FONT_SIZE = 0.2;

    var STATE_LABELS = {
        queued: '等待处理',
        downloading: '处理中...',
        completed: '已完成',
        failed: '下载失败',
        paused: '已暂停',
        missing: '未找到'
    };
    var STAGE_LABELS = {
        queued: '已入队',
        starting: '准备下载',
        downloading: '下载文件',
        download_subtitles: '下载字幕',
        download_video: '下载视频',
        download_audio: '下载音频',
        download_media: '下载音视频',
        merge_media: '合并音视频',
        embed_subtitles: '嵌入字幕',
        extract_audio: '转换音频',
        write_metadata: '写入信息',
        postprocessing: '处理文件',
        completed: '下载完成',
        failed: '下载失败',
        paused: '已暂停',
        missing: '未找到'
    };
    var ACTIVE_STATES = ['queued', 'downloading'];
    // 与 app.py 的 TASK_ACTION_STATES 对应：每种状态下可用的操作
    var TASK_ACTIONS_BY_STATE = {
        queued: ['pause', 'delete'],
        downloading: ['pause', 'restart', 'delete'],
        paused: ['resume', 'restart', 'delete'],
        failed: ['restart', 'delete'],
        completed: ['delete']
    };
    var PENDING_ACTION_LABELS = {
        pause: '正在暂停…',
        restart: '正在重启…'
    };

    var form = document.querySelector('.dl-form');
    var urlInput = document.querySelector('.dl-input');
    var submitButton = document.querySelector('.dl-submit');
    var typeBoxes = Array.prototype.slice.call(
        document.querySelectorAll('.dl-format input[type="checkbox"]')
    );
    var heroSection = document.querySelector('.dl-hero-inner');
    var listElement = document.querySelector('.dl-task-list');
    var demoRows = Array.prototype.map.call(
        document.querySelectorAll('.dl-task-demo'),
        function (item) { return item.cloneNode(true); }
    );
    var emptyElement = document.querySelector('.dl-task-empty');
    var countElement = document.querySelector('.dl-task-count');
    var overlay = document.querySelector('.dl-drawer-overlay');
    var drawerTitle = document.querySelector('.dl-drawer-title');
    var drawerFormat = document.querySelector('.dl-drawer-format');
    var drawerStatus = document.querySelector('.dl-drawer-status');
    var drawerLog = document.querySelector('.dl-drawer-log');
    var drawerAutoscroll = document.querySelector('.dl-drawer-autoscroll');
    var drawerClose = document.querySelector('.dl-drawer-close');
    var drawerThumbWrap = document.querySelector('.dl-drawer-thumb');
    var drawerThumb = document.querySelector('.dl-drawer-thumb img');
    var drawerVideoTitle = document.querySelector('.dl-drawer-video-title');
    var drawerUploader = document.querySelector('.dl-drawer-uploader');
    var drawerDuration = document.querySelector('.dl-drawer-duration');
    var drawerUrlText = document.querySelector('.dl-drawer-url-text');
    var drawerCopy = document.querySelector('.dl-drawer-copy');
    var drawerSummary = document.querySelector('.dl-drawer-summary');
    var drawerActions = document.querySelector('.dl-drawer-actions');
    var drawerConfirm = document.querySelector('.dl-drawer-confirm');
    var drawerConfirmText = document.querySelector('.dl-drawer-confirm-text');
    var drawerConfirmFiles = document.querySelector('.dl-drawer-confirm-files');
    var drawerConfirmCheckbox = document.querySelector('.dl-drawer-confirm-checkbox');
    var drawerActionMessage = document.querySelector('.dl-drawer-action-message');
    var modeLinks = Array.prototype.slice.call(document.querySelectorAll('.dl-mode'));
    var panels = {
        download: document.querySelector('[data-panel="download"]'),
        library: document.querySelector('[data-panel="library"]')
    };
    var stageFrame = document.querySelector('.dl-stage-frame');
    var stageCount = document.querySelector('.dl-stage-count-text');
    var nowIcon = document.querySelector('.dl-now-icon');
    var nowTitle = document.querySelector('.dl-now-title');
    var nowMeta = document.querySelector('.dl-now-meta');
    var nowDownload = document.querySelector('.dl-now-download');
    var nowSource = document.querySelector('.dl-now-source');
    var nowDescription = document.querySelector('.dl-now-description');
    var nowDescriptionText = document.querySelector('.dl-now-description-text');
    var tabButtons = Array.prototype.slice.call(document.querySelectorAll('.dl-tab'));
    var playlistItems = document.querySelector('.dl-playlist-items');
    var playlistEmpty = document.querySelector('.dl-playlist-empty');
    var playlistScroll = document.querySelector('.dl-playlist-scroll');
    var libraryActionMessage = document.querySelector('.dl-library-action-message');
    var mediaDeleteOverlay = document.querySelector('.dl-media-delete-overlay');
    var mediaDeleteDialog = document.querySelector('.dl-media-delete-dialog');
    var mediaDeleteCover = document.querySelector('.dl-media-delete-cover');
    var mediaDeleteName = document.querySelector('.dl-media-delete-name');
    var mediaDeleteMeta = document.querySelector('.dl-media-delete-meta');
    var mediaDeleteError = document.querySelector('.dl-media-delete-error');
    var mediaDeleteCancel = document.querySelector('[data-media-delete="cancel"]');
    var mediaDeleteConfirm = document.querySelector('[data-media-delete="confirm"]');

    if (!form || !listElement) return;

    confineLyricsScrolling();

    /** 任务本地缓存：{id, url}，最新的在前。 */
    var tasks = [];
    /** 最近一次 /api/task_info 返回的任务详情，按任务 ID 索引。 */
    var taskInfo = Object.create(null);
    var taskTimer = null;
    var logTimer = null;
    var renderedLogText = null;
    var taskLogAutoScroll = true;
    /** 视频元数据缓存，按源 URL 索引，避免重复调用较慢的解析接口。 */
    var metadata = Object.create(null);
    var openTaskId = null;
    /** 已交给下载器、尚未生效的暂停/重启请求，按任务 ID 索引。 */
    var pendingActions = Object.create(null);
    var actionBusy = false;
    /** 媒体库：按类型缓存的列表、当前标签与正在播放项。 */
    var mediaLibrary = null;
    var libraryTab = 'video';
    var currentMedia = null;
    var zwplayer = null;
    /** 当前 zwplayer 实例创建时的媒体类型（video/audio）。 */
    var playerType = null;
    /** 已挂好监听的 zwplayer 媒体元素；换源复用同一个元素。 */
    var mediaEl = null;
    /** 当前元素已载入元数据的条目，换源途中为 null，避免把旧进度记到新条目上。 */
    var loadedFilename = null;
    var lastProgressSaveAt = 0;
    /** 每次换源递增；晚到的上一条字幕或歌词据此丢弃。 */
    var textTrackSerial = 0;
    var libraryLoading = false;
    var mediaDeleteTarget = null;
    var mediaDeleteBusy = false;
    var mediaDeleteLastFocused = null;
    var libraryMessageTimer = null;
    var signedIn = false;
    var loginUrl = '/login';
    var lastFocused = null;

    function readStore() {
        try {
            var raw = JSON.parse(window.localStorage.getItem(TASKS_KEY) || '[]');
            if (!Array.isArray(raw)) return [];
            return raw
                .filter(function (item) {
                    return item && TASK_ID_PATTERN.test(item.id);
                })
                .map(function (item) {
                    return { id: item.id, url: typeof item.url === 'string' ? item.url : '' };
                })
                .slice(0, MAX_TASKS);
        } catch (error) {
            return [];
        }
    }

    function writeStore() {
        try {
            window.localStorage.setItem(TASKS_KEY, JSON.stringify(tasks.slice(0, MAX_TASKS)));
        } catch (error) {
            /* 隐私模式下写入失败不影响当前会话的列表展示 */
        }
    }

    function addTasks(ids, url) {
        var added = ids.filter(function (id) {
            return TASK_ID_PATTERN.test(id) && !tasks.some(function (task) {
                return task.id === id;
            });
        });
        if (!added.length) return added;
        tasks = added
            .map(function (id) {
                return { id: id, url: url || '' };
            })
            .concat(tasks)
            .slice(0, MAX_TASKS);
        writeStore();
        return added;
    }

    function removeTask(id) {
        tasks = tasks.filter(function (task) {
            return task.id !== id;
        });
        delete taskInfo[id];
        writeStore();
    }

    function formatLabel(taskId, info) {
        var type = (info && info.type) || (taskId.charAt(0) === 'v' ? 'video' : 'audio');
        return type === 'audio' ? '音频 MP3' : '视频 MP4';
    }

    function stateIcon(state) {
        if (state === 'completed') return '#i-check';
        if (state === 'downloading') return '#i-clock';
        if (state === 'paused') return '#i-pause';
        if (state === 'failed' || state === 'missing') return '#i-alert';
        return '#i-download';
    }

    function icon(href, extraClass) {
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', 'dl-icon' + (extraClass ? ' ' + extraClass : ''));
        svg.setAttribute('aria-hidden', 'true');
        var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
        use.setAttribute('href', href);
        svg.appendChild(use);
        return svg;
    }

    function formatBytes(value) {
        var size = Number(value);
        if (!isFinite(size) || size < 0) return '';
        var units = ['B', 'K', 'M', 'G', 'T'];
        var index = 0;
        while (size >= 1024 && index < units.length - 1) {
            size /= 1024;
            index += 1;
        }
        return size.toFixed(index === 0 ? 0 : 1) + units[index];
    }

    function statusText(taskId) {
        var info = taskInfo[taskId];
        if (!info) return STATE_LABELS.queued;
        var state = info.state || 'missing';
        var progress = info.progress || {};
        if (state === 'downloading' && PENDING_ACTION_LABELS[pendingActions[taskId]]) {
            return PENDING_ACTION_LABELS[pendingActions[taskId]];
        }
        if (state === 'paused') {
            var pausedAt = Number(progress.percent) || 0;
            return pausedAt > 0
                ? STATE_LABELS.paused + ' ' + pausedAt.toFixed(pausedAt % 1 === 0 ? 0 : 1) + '%'
                : STATE_LABELS.paused;
        }
        if (state === 'downloading') {
            var percent = Number(progress.percent) || 0;
            var stage = STAGE_LABELS[progress.stage] || STATE_LABELS.downloading;
            return stage + ' ' + percent.toFixed(percent % 1 === 0 ? 0 : 1) + '%';
        }
        if (state === 'completed') {
            var size = formatBytes(progress.final_size_bytes);
            return size ? STATE_LABELS.completed + ' · ' + size : STATE_LABELS.completed;
        }
        return STATE_LABELS[state] || state;
    }

    function buildRow(task) {
        var item = document.createElement('li');
        item.className = 'dl-task';
        item.dataset.task = task.id;
        item.dataset.state = 'queued';
        item.setAttribute('role', 'button');
        item.setAttribute('tabindex', '0');

        var iconWrap = document.createElement('span');
        iconWrap.className = 'dl-task-icon';
        iconWrap.appendChild(icon('#i-download'));

        var body = document.createElement('div');
        body.className = 'dl-task-body';

        var title = document.createElement('p');
        title.className = 'dl-task-title';

        var meta = document.createElement('div');
        meta.className = 'dl-task-meta';
        var formatSpan = document.createElement('span');
        formatSpan.className = 'dl-task-format';
        var dot = document.createElement('span');
        dot.textContent = '·';
        var stateSpan = document.createElement('span');
        stateSpan.className = 'dl-task-state';
        meta.append(formatSpan, dot, stateSpan);

        var progress = document.createElement('div');
        progress.className = 'dl-task-progress';
        var fill = document.createElement('div');
        fill.className = 'dl-task-progress-fill';
        progress.appendChild(fill);

        var actions = document.createElement('div');
        actions.className = 'dl-task-actions';

        body.append(title, meta, progress, actions);
        item.append(iconWrap, body);
        return item;
    }

    function renderRow(item, task) {
        var info = taskInfo[task.id];
        var state = (info && info.state) || 'queued';
        var progress = (info && info.progress) || {};
        var url = (info && info.url) || task.url || task.id;
        var percent = state === 'completed'
            ? 100
            : Math.max(0, Math.min(100, Number(progress.percent) || 0));

        item.dataset.state = state;

        var iconWrap = item.querySelector('.dl-task-icon');
        iconWrap.replaceChildren(
            icon(stateIcon(state), state === 'downloading' ? 'dl-spin' : '')
        );

        var title = item.querySelector('.dl-task-title');
        title.textContent = url;
        title.title = url;

        item.querySelector('.dl-task-format').textContent = formatLabel(task.id, info);
        item.querySelector('.dl-task-state').textContent = statusText(task.id);
        item.querySelector('.dl-task-progress-fill').style.width = percent + '%';

        var actions = item.querySelector('.dl-task-actions');
        actions.replaceChildren();
        if (info && info.download_url) {
            var download = document.createElement('a');
            download.className = 'dl-task-action';
            download.href = info.download_url;
            download.setAttribute('download', '');
            download.append(icon('#i-download'), document.createTextNode(' 下载文件'));
            actions.appendChild(download);
        }
        if (info && info.player_url) {
            var play = document.createElement('a');
            play.className = 'dl-task-action';
            play.href = info.player_url;
            play.append(icon('#i-play-circle'), document.createTextNode(' 播放'));
            actions.appendChild(play);
        }
    }

    function render() {
        var existing = Object.create(null);
        Array.prototype.forEach.call(listElement.children, function (item) {
            existing[item.dataset.task] = item;
        });

        var rows = tasks.length
            ? tasks.map(function (task) {
                var item = existing[task.id] || buildRow(task);
                renderRow(item, task);
                return item;
            })
            : demoRows.map(function (item) { return item.cloneNode(true); });
        listElement.replaceChildren.apply(listElement, rows);

        if (emptyElement) emptyElement.hidden = true;
        if (countElement) {
            countElement.textContent = tasks.length
                ? tasks.length + ' 个任务'
                : '支持的站点';
        }
        if (openTaskId) renderDrawerMeta();
    }

    function hasActiveTasks() {
        return tasks.some(function (task) {
            var info = taskInfo[task.id];
            return !info || ACTIVE_STATES.indexOf(info.state) !== -1;
        });
    }

    function scheduleTaskPoll() {
        if (taskTimer !== null) window.clearTimeout(taskTimer);
        if (!tasks.length || !hasActiveTasks()) {
            taskTimer = null;
            return;
        }
        taskTimer = window.setTimeout(pollTasks, TASK_POLL_MS);
    }

    async function pollTasks() {
        if (!tasks.length) return;
        try {
            var response = await fetch('/api/task_info', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    tasks: tasks.map(function (task) {
                        return task.id;
                    })
                })
            });
            if (!response.ok) throw new Error('HTTP ' + response.status);
            var data = await response.json();
            if (!data.success) throw new Error(data.msg || '查询任务失败');

            data.tasks.forEach(function (info) {
                if (info.exists === false && info.state === 'missing') {
                    // 任务文件已被清理，不再保留在本地列表中。
                    removeTask(info.task);
                    if (openTaskId === info.task) closeDrawer();
                    return;
                }
                taskInfo[info.task] = info;
                if (info.state !== 'downloading') delete pendingActions[info.task];
                var stored = tasks.find(function (task) {
                    return task.id === info.task;
                });
                if (stored && info.url && stored.url !== info.url) {
                    stored.url = info.url;
                    writeStore();
                }
            });
            render();
        } catch (error) {
            console.warn('获取任务进度失败:', error);
        }
        scheduleTaskPoll();
    }

    /* 任务详情抽屉 */

    function formatElapsed(value) {
        var parsed = Number(value);
        if (!isFinite(parsed) || parsed < 0) return '';
        var total = Math.round(parsed);
        return [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60]
            .map(function (part) {
                return String(part).padStart(2, '0');
            })
            .join(':');
    }

    function summaryText(progress) {
        var parts = [];
        var finalSize = formatBytes(progress.final_size_bytes);
        var elapsed = formatElapsed(progress.elapsed_seconds);
        var averageSpeed = formatBytes(progress.average_speed_bytes_per_second);
        if (finalSize && elapsed) parts.push(finalSize + ' in ' + elapsed);
        else if (finalSize) parts.push(finalSize);
        if (averageSpeed) parts.push(averageSpeed + '/s');
        return parts.join(' · ');
    }

    function setThumbnail(url, alt) {
        if (!url || !drawerThumb || !drawerThumbWrap) return;
        drawerThumb.src = url;
        drawerThumb.alt = alt || '视频缩略图';
        drawerThumb.hidden = false;
        drawerThumbWrap.classList.remove('is-placeholder');
        drawerThumb.addEventListener('error', function () {
            drawerThumb.hidden = true;
            drawerThumbWrap.classList.add('is-placeholder');
        }, { once: true });
    }

    function resetMetadataView() {
        drawerThumb.hidden = true;
        drawerThumb.removeAttribute('src');
        drawerThumbWrap.classList.add('is-placeholder');
        drawerVideoTitle.textContent = '正在获取标题…';
        drawerUploader.textContent = '元数据加载中';
        drawerDuration.textContent = '';
    }

    function renderMetadata(data) {
        if (!data) return;
        setThumbnail(data.thumbnail, data.title || '视频缩略图');
        drawerVideoTitle.textContent = data.title || '标题未知';
        drawerUploader.replaceChildren(
            icon('#i-user'),
            document.createTextNode(' ' + (data.uploader || data.platform || '未知平台'))
        );
        var totalSeconds = Math.round(Number(data.duration) || 0);
        var minutes = Math.floor(totalSeconds / 60);
        var seconds = totalSeconds % 60;
        drawerDuration.textContent = totalSeconds > 0
            ? minutes + ':' + String(seconds).padStart(2, '0')
            : '';
        if (data.title) renderDrawerMeta();
    }

    function updateMetadata(url, attempt) {
        attempt = attempt || 1;
        if (!url) return;
        var cached = metadata[url];
        if (cached && cached.state === 'ready') {
            renderMetadata(cached.data);
            return;
        }
        if (cached && cached.state === 'pending') return;
        metadata[url] = { state: 'pending' };

        var controller = new AbortController();
        var timeout = window.setTimeout(function () {
            controller.abort();
        }, METADATA_TIMEOUT_MS);

        fetch('/api/video_info_basic', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url }),
            signal: controller.signal
        })
            .then(function (response) {
                if (!response.ok) throw new Error('HTTP ' + response.status);
                return response.json();
            })
            .then(function (data) {
                if (!data.success) throw new Error(data.msg || '元数据查询失败');
                metadata[url] = { state: 'ready', data: data };
                if (currentTaskUrl() === url) renderMetadata(data);
            })
            .catch(function (error) {
                metadata[url] = { state: 'failed' };
                var isServerError = /^HTTP 5\d\d$/.test(error.message || '');
                var isRetryable = error.name === 'AbortError'
                    || error.name === 'TypeError'
                    || isServerError;
                if (isRetryable && attempt < METADATA_MAX_ATTEMPTS) {
                    if (currentTaskUrl() === url) {
                        drawerVideoTitle.textContent = '正在重试获取标题…';
                        drawerUploader.textContent = '元数据加载较慢，正在重试';
                    }
                    window.setTimeout(function () {
                        updateMetadata(url, attempt + 1);
                    }, METADATA_RETRY_DELAY_MS);
                    return;
                }
                console.warn('获取视频信息失败:', error);
                if (currentTaskUrl() !== url) return;
                if (drawerVideoTitle.textContent === '正在获取标题…'
                    || drawerVideoTitle.textContent === '正在重试获取标题…') {
                    drawerVideoTitle.textContent = '标题稍后显示';
                }
                drawerUploader.textContent = '元数据暂时不可用';
            })
            .finally(function () {
                window.clearTimeout(timeout);
            });
    }

    function currentTaskUrl() {
        if (!openTaskId) return '';
        var info = taskInfo[openTaskId];
        var stored = tasks.find(function (task) {
            return task.id === openTaskId;
        });
        return (info && (info.source_url || info.url)) || (stored && stored.url) || '';
    }

    function renderDrawerMeta() {
        var info = taskInfo[openTaskId];
        var stored = tasks.find(function (task) {
            return task.id === openTaskId;
        });
        if (!stored) return;

        var url = currentTaskUrl() || stored.id;
        var cached = metadata[url];
        var title = cached && cached.state === 'ready' && cached.data.title
            ? cached.data.title
            : url;

        drawerTitle.textContent = title;
        drawerTitle.title = title;
        drawerUrlText.textContent = url;
        drawerUrlText.title = url;
        drawerFormat.textContent = formatLabel(openTaskId, info);
        drawerStatus.textContent = statusText(openTaskId);

        var summary = info && info.state === 'completed'
            ? summaryText(info.progress || {})
            : '';
        drawerSummary.textContent = summary;
        drawerSummary.hidden = !summary;
        renderDrawerActions(info);
        // 状态、摘要或操作按钮改变高度时，跟随模式仍应贴住日志末尾。
        if (taskLogAutoScroll || isTaskLogNearBottom()) {
            drawerLog.scrollTop = drawerLog.scrollHeight;
        }
    }

    function renderDrawerActions(info) {
        var state = info && info.state;
        var allowed = TASK_ACTIONS_BY_STATE[state] || [];
        var pending = Boolean(pendingActions[openTaskId]);
        Array.prototype.forEach.call(drawerActions.querySelectorAll('[data-action]'), function (button) {
            button.hidden = allowed.indexOf(button.dataset.action) === -1;
            // 请求已交给下载器时，除删除外暂不允许重复操作
            button.disabled = actionBusy || (pending && button.dataset.action !== 'delete');
        });
        drawerActions.hidden = !allowed.length || !drawerConfirm.hidden;
    }

    function showActionMessage(text, isError) {
        drawerActionMessage.textContent = text || '';
        drawerActionMessage.hidden = !text;
        drawerActionMessage.classList.toggle('is-error', Boolean(isError));
    }

    function hideDeleteConfirm() {
        drawerConfirm.hidden = true;
        drawerConfirmCheckbox.checked = false;
        renderDrawerActions(taskInfo[openTaskId]);
    }

    function showDeleteConfirm() {
        var info = taskInfo[openTaskId];
        var completed = info && info.state === 'completed';
        drawerConfirmText.textContent = completed
            ? '删除后任务将从列表中移除。'
            : '删除后任务将停止，未完成的临时文件会一并清除。';
        // 只有已完成的任务才有「已下载的文件」可选，默认保留
        drawerConfirmFiles.hidden = !completed;
        drawerConfirmCheckbox.checked = false;
        drawerConfirm.hidden = false;
        drawerActions.hidden = true;
        showActionMessage('');
    }

    async function runTaskAction(taskId, action, deleteFiles) {
        actionBusy = true;
        renderDrawerActions(taskInfo[taskId]);
        showActionMessage('');
        try {
            var response = await fetch('/api/task_action', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ task: taskId, action: action, delete_files: Boolean(deleteFiles) })
            });
            var data = {};
            try {
                data = await response.json();
            } catch (error) {
                data = {};
            }
            if (!response.ok || !data.success) {
                if (openTaskId === taskId) showActionMessage(data.msg || '操作失败，请稍后重试', true);
                pollTasks();
                return;
            }
            if (action === 'delete') {
                // 正在下载的任务由下载器异步清理，但归属已解除，列表里立即移除
                removeTask(taskId);
                if (openTaskId === taskId) closeDrawer();
                render();
                return;
            }
            if (response.status === 202) {
                pendingActions[taskId] = action;
            } else if (taskInfo[taskId] && data.state) {
                taskInfo[taskId].state = data.state;
            }
            render();
            pollTasks();
            if (openTaskId === taskId) pollLog();
        } catch (error) {
            if (openTaskId === taskId) showActionMessage('网络异常，操作未完成', true);
        } finally {
            actionBusy = false;
            if (openTaskId === taskId) renderDrawerActions(taskInfo[taskId]);
        }
    }

    drawerActions.addEventListener('click', function (event) {
        var button = event.target.closest('[data-action]');
        if (!button || button.disabled || !openTaskId) return;
        if (button.dataset.action === 'delete') {
            showDeleteConfirm();
            return;
        }
        runTaskAction(openTaskId, button.dataset.action, false);
    });

    drawerConfirm.addEventListener('click', function (event) {
        var button = event.target.closest('[data-confirm]');
        if (!button || !openTaskId) return;
        if (button.dataset.confirm === 'cancel') {
            hideDeleteConfirm();
            return;
        }
        var deleteFiles = !drawerConfirmFiles.hidden && drawerConfirmCheckbox.checked;
        drawerConfirm.hidden = true;
        runTaskAction(openTaskId, 'delete', deleteFiles);
    });

    function renderLog(text) {
        var normalizedText = String(text || '');
        if (normalizedText === renderedLogText) return;

        var stickToBottom = taskLogAutoScroll || isTaskLogNearBottom();
        var previousScrollTop = drawerLog.scrollTop;
        var lines = normalizedText.split('\n').filter(function (line) {
            return line.trim() !== '';
        });
        renderedLogText = normalizedText;
        if (!lines.length) {
            drawerLog.replaceChildren(document.createTextNode('等待下载日志…'));
            return;
        }
        drawerLog.replaceChildren.apply(drawerLog, lines.map(function (line, index) {
            var row = document.createElement('p');
            var number = document.createElement('span');
            number.className = 'dl-log-index';
            number.textContent = String(index + 1).padStart(2, '0');
            row.append(number, document.createTextNode(line));
            return row;
        }));
        drawerLog.scrollTop = stickToBottom
            ? drawerLog.scrollHeight
            : previousScrollTop;
    }

    function isTaskLogNearBottom() {
        return drawerLog.scrollHeight - drawerLog.scrollTop
            - drawerLog.clientHeight < 48;
    }

    function updateTaskLogAutoscroll() {
        drawerAutoscroll.classList.toggle('is-active', taskLogAutoScroll);
        drawerAutoscroll.setAttribute('aria-pressed', String(taskLogAutoScroll));
        drawerAutoscroll.textContent = '自动滚动：' + (taskLogAutoScroll ? '开' : '关');
    }

    drawerAutoscroll.addEventListener('click', function () {
        taskLogAutoScroll = !taskLogAutoScroll;
        updateTaskLogAutoscroll();
        if (taskLogAutoScroll) drawerLog.scrollTop = drawerLog.scrollHeight;
    });

    drawerLog.addEventListener('scroll', function () {
        if (taskLogAutoScroll && !isTaskLogNearBottom()) {
            taskLogAutoScroll = false;
            updateTaskLogAutoscroll();
        }
    }, { passive: true });

    function scheduleLogPoll() {
        if (logTimer !== null) window.clearTimeout(logTimer);
        logTimer = null;
        if (!openTaskId) return;
        var info = taskInfo[openTaskId];
        if (info && ACTIVE_STATES.indexOf(info.state) === -1) return;
        logTimer = window.setTimeout(pollLog, LOG_POLL_MS);
    }

    async function pollLog() {
        var taskId = openTaskId;
        if (!taskId) return;
        try {
            var response = await fetch('/api/task_log', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tasks: [taskId] })
            });
            if (!response.ok) throw new Error('HTTP ' + response.status);
            var data = await response.json();
            if (!data.success) throw new Error(data.msg || '读取日志失败');
            if (openTaskId === taskId) renderLog(data.text);
        } catch (error) {
            console.warn('获取任务日志失败:', error);
        }
        scheduleLogPoll();
    }

    function openDrawer(taskId) {
        openTaskId = taskId;
        lastFocused = document.activeElement;
        overlay.hidden = false;
        drawerConfirm.hidden = true;
        showActionMessage('');
        resetMetadataView();
        renderDrawerMeta();

        var info = taskInfo[taskId];
        // task_info 为 YouTube 链接提供的封面可以先行展示，无需等待元数据接口。
        if (info && info.thumbnail) setThumbnail(info.thumbnail);
        updateMetadata(currentTaskUrl());

        renderedLogText = null;
        taskLogAutoScroll = true;
        updateTaskLogAutoscroll();
        drawerLog.replaceChildren(document.createTextNode('正在读取日志…'));
        drawerClose.focus();
        pollLog();
    }

    function closeDrawer() {
        if (logTimer !== null) window.clearTimeout(logTimer);
        logTimer = null;
        openTaskId = null;
        overlay.hidden = true;
        if (lastFocused && document.contains(lastFocused)) lastFocused.focus();
        lastFocused = null;
    }

    /* navigator.clipboard 仅在安全上下文可用，局域网 http 访问需回退到 execCommand。 */
    function fallbackCopyText(value) {
        var textarea = document.createElement('textarea');
        textarea.value = value;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.top = '0';
        textarea.style.left = '0';
        textarea.style.opacity = '0';
        textarea.style.fontSize = '16px';
        document.body.appendChild(textarea);
        try {
            textarea.select();
            textarea.setSelectionRange(0, value.length);
            if (!document.execCommand('copy')) throw new Error('浏览器拒绝复制');
        } finally {
            textarea.remove();
        }
    }

    async function copyText(value) {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(value);
            return;
        }
        fallbackCopyText(value);
    }

    var copyResetTimer = null;
    drawerCopy.addEventListener('click', async function () {
        var value = drawerUrlText.textContent;
        if (!value) return;
        try {
            await copyText(value);
            drawerCopy.title = '已复制到剪贴板';
        } catch (error) {
            console.error('复制失败:', error);
            drawerCopy.title = '复制失败，请长按链接手动复制';
        }
        drawerCopy.classList.add('is-copied');
        if (copyResetTimer !== null) window.clearTimeout(copyResetTimer);
        copyResetTimer = window.setTimeout(function () {
            copyResetTimer = null;
            drawerCopy.title = '复制链接';
            drawerCopy.classList.remove('is-copied');
        }, 2000);
    });

    listElement.addEventListener('click', function (event) {
        var item = event.target.closest('.dl-task');
        if (!item || event.target.closest('a')) return;
        openDrawer(item.dataset.task);
    });

    listElement.addEventListener('keydown', function (event) {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        var item = event.target.closest('.dl-task');
        if (!item || event.target.closest('a')) return;
        event.preventDefault();
        openDrawer(item.dataset.task);
    });

    overlay.addEventListener('click', function (event) {
        if (event.target === overlay) closeDrawer();
    });
    drawerClose.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape' && openTaskId) closeDrawer();
    });

    /* 媒体库 */

    function formatDuration(value) {
        var total = Math.round(Number(value) || 0);
        if (!total) return '';
        var hours = Math.floor(total / 3600);
        var minutes = Math.floor((total % 3600) / 60);
        var seconds = total % 60;
        var parts = hours
            ? [hours, minutes, seconds]
            : [minutes, seconds];
        return parts
            .map(function (part, index) {
                return index === 0 ? String(part) : String(part).padStart(2, '0');
            })
            .join(':');
    }

    function mediaFormatLabel(item) {
        var parts = [(item.extension || '').toUpperCase()];
        if (item.type === 'video' && item.height) parts.push(item.height + 'p');
        else if (item.size_bytes) parts.push(formatBytes(item.size_bytes));
        return parts.filter(Boolean).join(' · ');
    }

    function mediaMetaLine(item) {
        return [mediaFormatLabel(item), formatDuration(item.duration)]
            .filter(Boolean)
            .join(' · ');
    }

    function readStorage(key) {
        try {
            return window.localStorage.getItem(key);
        } catch (error) {
            return null;
        }
    }

    function writeStorage(key, value) {
        try {
            if (value === null) window.localStorage.removeItem(key);
            else window.localStorage.setItem(key, value);
        } catch (error) {
            /* 隐私模式下无法记忆，不影响播放 */
        }
    }

    function destroyPlayer() {
        textTrackSerial += 1;
        saveProgress(true);
        clearMediaSession();
        mediaEl = null;
        loadedFilename = null;
        playerType = null;
        if (zwplayer) {
            try {
                zwplayer.destroy();
            } catch (error) {
                console.warn('销毁播放器失败:', error);
            }
            zwplayer = null;
        }
        // 首次播放时也要清掉模板里的占位节点，否则会留下重复的挂载点。
        stageFrame.replaceChildren();
    }

    function createPlayer(item, autoplay) {
        var mount = document.createElement('div');
        mount.id = 'dl-player';
        stageFrame.appendChild(mount);

        if (typeof window.ZWPlayer !== 'function') {
            console.warn('ZWPlayer 未加载');
            return;
        }
        try {
            zwplayer = new window.ZWPlayer({
                playerElm: mount,
                url: item.url,
                poster: item.poster || '',
                fluid: true,
                autoplay: autoplay,
                // 打开带 file 参数的链接会自动播放；浏览器拦截有声自动播放时，
                // zwplayer 会改为静音播放，保留提示才能让用户一点就开声音。
                disableMutedConfirm: false,
                // 标准模式下倍速按钮默认不显示，需显式打开。
                speedButton: true,
                mediaKind: item.type === 'audio' ? 'audio' : 'video',
                // 音频会进入 zwplayer 音乐模式，它自带的 Media Session 指向
                // 内部播放列表；关掉后由 attachMediaSession 统一接管锁屏控制。
                music: {
                    mediaSession: false,
                    title: item.title || '',
                    artist: item.artist || ''
                },
                onready: function () {
                    attachMediaElement(this.videoEl);
                }
            });
        } catch (error) {
            console.error('初始化播放器失败:', error);
            return;
        }
        playerType = item.type;
        attachMediaElement(zwplayer.videoEl);
        applySubtitleDefaults();
        installSubtitleScaling();
        loadTextTracks(item);
    }

    function confineLyricsScrolling() {
        // zwplayer 每换一句歌词就对当前行调用 scrollIntoView({block: 'center'})，
        // 它会连带滚动所有可滚动的祖先，用户往下翻页时会被拽回播放器。
        // 歌词行随切歌、切换面板重建，逐个元素改不全，所以在原型上只接管歌词容器里的元素，
        // 让它们只滚动歌词容器本身；其他元素仍走浏览器原生行为。
        var nativeScrollIntoView = window.Element && Element.prototype.scrollIntoView;
        if (typeof nativeScrollIntoView !== 'function') return;
        Element.prototype.scrollIntoView = function (options) {
            var container = typeof this.closest === 'function'
                ? this.closest('.zwp-music-lyrics')
                : null;
            if (!container) return nativeScrollIntoView.apply(this, arguments);
            var lineRect = this.getBoundingClientRect();
            var containerRect = container.getBoundingClientRect();
            var offset = lineRect.top - containerRect.top
                - (container.clientHeight - lineRect.height) / 2;
            container.scrollBy({
                top: offset,
                behavior: options && options.behavior === 'smooth' ? 'smooth' : 'auto'
            });
        };
    }

    function installSubtitleScaling() {
        // zwplayer 的 updateSubtitlePosition 只负责算字幕字号：基准为播放器宽度的 5%，
        // 但夹在 14–32px 之间，宽于 640px 后全屏与页面内一样大。它挂在实例上，
        // 字幕层尺寸变化（含进出全屏）时经 this 调用，替换实例属性即可接管（内部方法，升级时需复查）。
        if (!zwplayer || typeof zwplayer.updateSubtitlePosition !== 'function') return;
        var player = zwplayer;
        player.updateSubtitlePosition = function () {
            var layer = player.subtitleShow;
            if (!layer || !player.subtitles) return;
            var settings = player._subtitleSettings || {};
            // 「等比缩放」关闭时保持 zwplayer 的行为：不改字号
            if (settings.primary && settings.primary.scale === false) return;
            var width = layer.offsetWidth;
            var height = layer.offsetHeight;
            if (!width || !height) return;
            var base = Math.max(
                SUBTITLE_BASE_MIN_PX,
                Math.min(width * SUBTITLE_BASE_WIDTH_RATIO, height * SUBTITLE_BASE_HEIGHT_RATIO)
            );
            [['1', 'primary'], ['2', 'secondary']].forEach(function (pair) {
                var element = player['subtitle' + pair[0]];
                if (!player.subtitles[pair[0]] || !element) return;
                var track = settings[pair[1]];
                var level = track && track.fontSize ? parseFloat(track.fontSize) : SUBTITLE_MEDIUM_FONT_SIZE;
                var size = Math.min(SUBTITLE_MAX_PX, base * level / SUBTITLE_MEDIUM_FONT_SIZE);
                element.style.fontSize = size.toFixed(2) + 'px';
            });
        };
    }

    function applySubtitleDefaults() {
        // zwplayer 没有字幕样式的构造参数，只能改实例上的 _subtitleSettings（内部字段，
        // 升级时需复查）。设置面板打开时才按它生成，字号在每次渲染时读取，所以建好实例就改。
        var settings = zwplayer && zwplayer._subtitleSettings;
        if (!settings || !settings.primary) return;
        settings.primary.fontSize = SUBTITLE_FONT_SIZE_SMALL;
        // 背景不透明度拉到最低：字幕直接叠在画面上，不带黑底
        settings.primary.bgOpacity = 0;
        if (settings.secondary) settings.secondary.bgOpacity = 0;
        if (typeof zwplayer._applyAllSubtitleSettings === 'function') {
            zwplayer._applyAllSubtitleSettings();
        }
    }

    /* 字幕与歌词 */

    function fetchText(url) {
        return fetch(url, { credentials: 'same-origin' }).then(function (response) {
            if (!response.ok) throw new Error('HTTP ' + response.status);
            return response.text();
        });
    }

    function isCurrentTextTrack(serial, item) {
        return serial === textTrackSerial && currentMedia === item && Boolean(zwplayer);
    }

    function loadTextTracks(item) {
        // 不把地址直接交给 zwplayer 加载：它的异步请求不核对当前片源，快速切换时
        // 上一条的字幕或歌词会挂到新条目上。先取回文本，确认仍是同一条再交给它。
        var serial = ++textTrackSerial;
        if (item.type === 'audio') loadLyrics(item, serial);
        else loadSubtitles(item, serial);
    }

    function loadLyrics(item, serial) {
        // play(url) 换源不会清掉音乐模式的歌词，先清空，免得上一首的歌词继续滚动
        if (typeof zwplayer.setLyrics === 'function') zwplayer.setLyrics('');
        if (!item.lyrics_url) return;
        fetchText(item.lyrics_url).then(function (text) {
            if (!isCurrentTextTrack(serial, item)) return;
            // zwplayer 只在内容带 [mm:ss 时间戳时按 LRC 文本解析，否则会把它当作地址去请求
            if (typeof zwplayer.setLyrics !== 'function' || !LRC_TIMESTAMP.test(text)) return;
            zwplayer.setLyrics(text);
        }).catch(function (error) {
            console.warn('加载歌词失败:', error);
        });
    }

    function loadSubtitles(item, serial) {
        var tracks = Array.isArray(item.subtitles) ? item.subtitles : [];
        if (!tracks.length) return;
        Promise.all(tracks.map(function (track) {
            return fetchText(track.url).catch(function (error) {
                console.warn('加载字幕失败:', track.label, error);
                return '';
            });
        })).then(function (texts) {
            if (!isCurrentTextTrack(serial, item)) return;
            if (typeof zwplayer.addSubtitle !== 'function') return;
            var primaryAssigned = false;
            tracks.forEach(function (track, index) {
                if (!texts[index]) return;
                // 第一条可用轨道（后端按简体、繁体、中文、英文排序）默认显示，
                // 其余只进 CC 菜单，由用户切换或开启双语。
                var pos = primaryAssigned ? '' : '1';
                primaryAssigned = true;
                try {
                    zwplayer.addSubtitle(texts[index], pos, track.label);
                } catch (error) {
                    console.warn('添加字幕失败:', track.label, error);
                }
            });
        });
    }

    function updateLocationForMedia(item) {
        // 地址栏跟随当前条目，与任务完成后的直达链接同一格式，刷新或复制都能回到这一条。
        var params = new URLSearchParams();
        params.set('file', item.filename);
        window.history.replaceState({}, '', '/player?' + params.toString());
    }

    function clearMediaLocation() {
        var params = new URLSearchParams();
        if (libraryTab === 'audio') params.set('tab', 'audio');
        var query = params.toString();
        window.history.replaceState({}, '', '/player' + (query ? '?' + query : ''));
    }

    function playMedia(item, options) {
        if (!item) return;
        var autoplay = Boolean(options && options.autoplay);
        saveProgress(true);
        currentMedia = item;
        loadedFilename = null;
        lastProgressSaveAt = 0;
        renderNowPlaying(item);
        renderPlaylist();
        revealCurrentPlaylistItem();
        // 首次载入与深链定位不改地址；用户切换、自动下一个、锁屏切换才同步。
        if (autoplay) updateLocationForMedia(item);

        if (zwplayer && autoplay && playerType === item.type) {
            // 复用同一个媒体元素换源：iOS 上新建的元素没有用户手势授权，
            // 自动播放下一条或锁屏续播会被拦截。
            // 视频与音频之间不能复用：zwplayer 的 play(url) 不会在标准模式和
            // 音乐模式之间切换，跨类型必然来自用户点击，重建时仍有手势授权。
            try {
                // play(url) 会先 stop()，连同上一条的字幕一起清掉
                zwplayer.play(item.url);
                loadTextTracks(item);
                if (mediaEl) mediaEl.poster = item.poster || '';
                // play(url) 只换音源，音乐模式的封面、模糊背景、标题和作者
                // 要另外通知 zwplayer，否则一直停留在第一首。
                if (item.type === 'audio' && typeof zwplayer.setMusicTrack === 'function') {
                    zwplayer.setMusicTrack({
                        title: item.title || '',
                        artist: item.artist || '',
                        poster: item.poster || ''
                    });
                }
            } catch (error) {
                console.error('切换媒体失败:', error);
            }
        } else {
            destroyPlayer();
            createPlayer(item, autoplay);
        }
        updateSessionMetadata();
    }

    /* 媒体元素：进度、倍速、自动下一个 */

    function attachMediaElement(element) {
        if (!element || element === mediaEl) return;
        mediaEl = element;
        element.addEventListener('loadedmetadata', handleLoadedMetadata);
        element.addEventListener('timeupdate', handleTimeUpdate);
        element.addEventListener('canplay', handleCanPlay);
        element.addEventListener('play', handlePlay);
        element.addEventListener('pause', handlePause);
        element.addEventListener('ended', handleEnded);
        element.addEventListener('ratechange', handleRateChange);
        element.addEventListener('durationchange', handleDurationChange);
        if (element.readyState >= 1) handleLoadedMetadata({ currentTarget: element });
    }

    function isCurrentElement(event) {
        // 已销毁的旧元素可能还会补发事件，一律忽略。
        return Boolean(mediaEl) && event.currentTarget === mediaEl;
    }

    function progressKey(item) {
        return PROGRESS_KEY_PREFIX + item.filename;
    }

    function saveProgress(force) {
        if (!mediaEl || !currentMedia || loadedFilename !== currentMedia.filename) return;
        var now = Date.now();
        if (!force && now - lastProgressSaveAt < PROGRESS_SAVE_INTERVAL_MS) return;
        var time = mediaEl.currentTime;
        var duration = mediaEl.duration;
        if (!isFinite(time) || time <= 0) return;
        lastProgressSaveAt = now;
        if (isFinite(duration) && duration > 0 && duration - time <= PLAYBACK_END_THRESHOLD) {
            writeStorage(progressKey(currentMedia), null);
            return;
        }
        writeStorage(progressKey(currentMedia), String(time));
    }

    function restoreProgress() {
        var saved = Number(readStorage(progressKey(currentMedia)));
        if (!isFinite(saved) || saved <= 0) return;
        var duration = mediaEl.duration;
        if (isFinite(duration) && duration > 0 && saved >= duration - PLAYBACK_END_THRESHOLD) {
            writeStorage(progressKey(currentMedia), null);
            return;
        }
        mediaEl.currentTime = saved;
    }

    function storedPlaybackRate() {
        var rate = Number(readStorage(PLAYBACK_RATE_KEY));
        return PLAYBACK_RATES.indexOf(rate) === -1 ? 1 : rate;
    }

    function applyPlaybackRate() {
        // 换源时 zwplayer 会把倍速菜单复位成 1x，浏览器也会重置 playbackRate。
        var rate = storedPlaybackRate();
        mediaEl.defaultPlaybackRate = rate;
        mediaEl.playbackRate = rate;
        syncSpeedMenu();
    }

    function syncSpeedMenu() {
        // 首次创建播放器时倍速按钮晚于 loadedmetadata 生成，canplay/play 时再对齐一次。
        if (mediaEl && zwplayer && typeof zwplayer._syncSpeedBtnUI === 'function') {
            zwplayer._syncSpeedBtnUI(mediaEl.playbackRate);
        }
    }

    function handleCanPlay(event) {
        if (!isCurrentElement(event)) return;
        syncSpeedMenu();
    }

    function handleLoadedMetadata(event) {
        if (!isCurrentElement(event) || !currentMedia) return;
        loadedFilename = currentMedia.filename;
        restoreProgress();
        applyPlaybackRate();
        updatePositionState();
    }

    function handleTimeUpdate(event) {
        if (!isCurrentElement(event)) return;
        saveProgress(false);
        updatePositionState();
    }

    function handlePlay(event) {
        if (!isCurrentElement(event)) return;
        syncSpeedMenu();
        attachMediaSession();
    }

    function handlePause(event) {
        if (!isCurrentElement(event)) return;
        saveProgress(true);
        setSessionPlaybackState('paused');
    }

    function handleRateChange(event) {
        if (!isCurrentElement(event)) return;
        // 换源途中的复位不代表用户选择，只记忆元数据载入后的改动。
        if (currentMedia && loadedFilename === currentMedia.filename
            && PLAYBACK_RATES.indexOf(mediaEl.playbackRate) !== -1) {
            writeStorage(PLAYBACK_RATE_KEY, String(mediaEl.playbackRate));
        }
        updatePositionState();
    }

    function handleDurationChange(event) {
        if (!isCurrentElement(event)) return;
        updatePositionState();
    }

    function adjacentMedia(offset) {
        // 以正在播放条目的类型取列表，用户切到另一个标签也不会串到另一类。
        if (!currentMedia || !mediaLibrary) return null;
        var list = mediaLibrary[currentMedia.type] || [];
        for (var index = 0; index < list.length; index += 1) {
            if (list[index].filename === currentMedia.filename) {
                return list[index + offset] || null;
            }
        }
        return null;
    }

    function handleEnded(event) {
        if (!isCurrentElement(event) || !currentMedia) return;
        writeStorage(progressKey(currentMedia), null);
        var next = adjacentMedia(1);
        // 播到列表末尾就停下，与旧播放页一致，不循环。
        if (next) playMedia(next, { autoplay: true });
        else setSessionPlaybackState('none');
    }

    window.addEventListener('pagehide', function () {
        saveProgress(true);
    });
    document.addEventListener('visibilitychange', function () {
        // iOS 切到后台后页面可能被直接回收，趁隐藏时落盘。
        if (document.visibilityState === 'hidden') saveProgress(true);
    });

    /* Media Session：锁屏与后台控制 */

    var mediaSession = navigator.mediaSession || null;

    function setSessionAction(name, handler) {
        if (!mediaSession) return;
        try {
            mediaSession.setActionHandler(name, handler);
        } catch (error) {
            /* 浏览器不支持该动作 */
        }
    }

    function setSessionPlaybackState(state) {
        if (!mediaSession) return;
        try {
            mediaSession.playbackState = state;
        } catch (error) {
            /* 忽略 */
        }
    }

    function updateSessionMetadata() {
        if (!mediaSession || !currentMedia || typeof window.MediaMetadata !== 'function') return;
        try {
            mediaSession.metadata = new window.MediaMetadata({
                title: currentMedia.title || '',
                artist: currentMedia.artist || '',
                artwork: currentMedia.poster ? [{ src: currentMedia.poster }] : []
            });
        } catch (error) {
            /* 部分浏览器不接受封面地址 */
        }
    }

    function updatePositionState() {
        if (!mediaSession || !mediaEl || typeof mediaSession.setPositionState !== 'function') return;
        var duration = mediaEl.duration;
        var position = mediaEl.currentTime;
        try {
            if (!isFinite(duration) || duration <= 0 || !isFinite(position)) {
                mediaSession.setPositionState();
                return;
            }
            mediaSession.setPositionState({
                duration: duration,
                playbackRate: mediaEl.playbackRate || 1,
                position: Math.max(0, Math.min(position, duration))
            });
        } catch (error) {
            /* 部分浏览器只实现了部分 Media Session */
        }
    }

    function seekTo(seconds) {
        if (!mediaEl) return;
        var duration = mediaEl.duration;
        if (!isFinite(seconds) || !isFinite(duration) || duration <= 0) return;
        mediaEl.currentTime = Math.max(0, Math.min(seconds, duration));
        updatePositionState();
    }

    function resumePlayback() {
        if (zwplayer && typeof zwplayer.resume === 'function') {
            zwplayer.resume();
            return;
        }
        if (mediaEl) {
            var result = mediaEl.play();
            if (result && result.catch) result.catch(function () {
                setSessionPlaybackState('paused');
            });
        }
    }

    function attachMediaSession() {
        try {
            // Safari 的实验性 API：声明为播放类会话，锁屏后不被当作可打断的声音。
            if (navigator.audioSession) navigator.audioSession.type = 'playback';
        } catch (error) {
            /* 不支持时仍可前台播放 */
        }
        if (!mediaSession) return;
        updateSessionMetadata();
        setSessionPlaybackState('playing');
        setSessionAction('play', resumePlayback);
        setSessionAction('pause', function () {
            if (zwplayer && typeof zwplayer.pause === 'function') zwplayer.pause();
            else if (mediaEl) mediaEl.pause();
        });
        setSessionAction('seekbackward', function (details) {
            if (mediaEl) seekTo(mediaEl.currentTime - ((details && details.seekOffset) || SEEK_OFFSET_SECONDS));
        });
        setSessionAction('seekforward', function (details) {
            if (mediaEl) seekTo(mediaEl.currentTime + ((details && details.seekOffset) || SEEK_OFFSET_SECONDS));
        });
        setSessionAction('seekto', function (details) {
            if (details) seekTo(details.seekTime);
        });
        setSessionAction('previoustrack', function () {
            var previous = adjacentMedia(-1);
            if (previous) playMedia(previous, { autoplay: true });
            else seekTo(0);
        });
        setSessionAction('nexttrack', function () {
            var next = adjacentMedia(1);
            if (next) playMedia(next, { autoplay: true });
        });
        updatePositionState();
    }

    function clearMediaSession() {
        if (!mediaSession) return;
        ['play', 'pause', 'seekbackward', 'seekforward', 'seekto', 'previoustrack', 'nexttrack']
            .forEach(function (name) {
                setSessionAction(name, null);
            });
        try {
            mediaSession.metadata = null;
        } catch (error) {
            /* 忽略 */
        }
        setSessionPlaybackState('none');
    }

    function renderNowSource(item) {
        var sourceUrl = (item && item.source_url) || '';
        nowSource.hidden = !sourceUrl;
        if (sourceUrl) nowSource.href = sourceUrl;
        else nowSource.removeAttribute('href');

        var description = (item && item.description) || '';
        nowDescriptionText.textContent = description;
        nowDescription.hidden = !description;
        // 默认折叠；切换媒体时也恢复折叠，避免沿用上一条的展开状态。
        nowDescription.open = false;
    }

    function renderNowPlaying(item) {
        renderNowSource(item);
        if (!item) {
            nowTitle.textContent = '未选择媒体';
            nowMeta.textContent = '从右侧播放列表中选择已下载的媒体';
            nowDownload.hidden = true;
            return;
        }
        nowIcon.replaceChildren(icon(item.type === 'audio' ? '#i-music' : '#i-video'));
        nowTitle.textContent = item.title;
        nowTitle.title = item.title;
        nowMeta.textContent = [item.artist, mediaMetaLine(item)]
            .filter(Boolean)
            .join(' · ');
        nowDownload.href = item.download_url;
        nowDownload.hidden = false;
    }

    function createPlaylistCover(item) {
        var image = document.createElement('img');
        var candidates = Array.isArray(item.thumbnail_candidates)
            ? item.thumbnail_candidates.filter(Boolean)
            : [];
        var candidateIndex = 0;
        image.className = 'dl-playlist-item-cover';
        image.alt = '';
        image.setAttribute('aria-hidden', 'true');
        image.loading = 'lazy';
        image.decoding = 'async';
        image.src = candidates[0] || '/static/images/media-cover-default.svg';
        image.addEventListener('error', function () {
            candidateIndex += 1;
            if (candidateIndex < candidates.length) {
                image.src = candidates[candidateIndex];
            } else if (!image.src.endsWith('/static/images/media-cover-default.svg')) {
                image.src = '/static/images/media-cover-default.svg';
            }
        });
        return image;
    }

    function showLibraryMessage(text) {
        if (libraryMessageTimer !== null) window.clearTimeout(libraryMessageTimer);
        libraryMessageTimer = null;
        libraryActionMessage.textContent = text || '';
        libraryActionMessage.hidden = !text;
        if (text) {
            libraryMessageTimer = window.setTimeout(function () {
                libraryMessageTimer = null;
                libraryActionMessage.hidden = true;
            }, 3000);
        }
    }

    function setMediaDeleteError(text) {
        mediaDeleteError.textContent = text || '';
        mediaDeleteError.hidden = !text;
    }

    function setMediaDeleteCover(item) {
        var candidates = Array.isArray(item.thumbnail_candidates)
            ? item.thumbnail_candidates.filter(Boolean)
            : [];
        var candidateIndex = 0;
        candidates.push('/static/images/media-cover-default.svg');
        mediaDeleteCover.onerror = function () {
            candidateIndex += 1;
            if (candidateIndex < candidates.length) {
                mediaDeleteCover.src = candidates[candidateIndex];
            }
        };
        mediaDeleteCover.src = candidates[0];
    }

    function openMediaDelete(item, trigger) {
        if (!item || mediaDeleteBusy) return;
        mediaDeleteTarget = item;
        mediaDeleteLastFocused = trigger || document.activeElement;
        mediaDeleteName.textContent = item.title || item.filename;
        mediaDeleteName.title = item.title || item.filename;
        mediaDeleteMeta.textContent = [item.filename, formatBytes(item.size_bytes)]
            .filter(Boolean)
            .join(' · ');
        setMediaDeleteCover(item);
        setMediaDeleteError('');
        mediaDeleteOverlay.hidden = false;
        mediaDeleteConfirm.focus();
    }

    function closeMediaDelete(force) {
        if (mediaDeleteBusy && !force) return;
        mediaDeleteOverlay.hidden = true;
        mediaDeleteTarget = null;
        setMediaDeleteError('');
        mediaDeleteCancel.disabled = false;
        mediaDeleteConfirm.disabled = false;
        mediaDeleteConfirm.textContent = '永久删除';
        if (mediaDeleteLastFocused && document.contains(mediaDeleteLastFocused)) {
            mediaDeleteLastFocused.focus();
        } else {
            var fallback = playlistItems.querySelector(
                '.dl-playlist-item[aria-current="true"], .dl-playlist-item'
            );
            if (fallback) fallback.focus();
        }
        mediaDeleteLastFocused = null;
    }

    function nextMediaAfter(item) {
        var list = mediaLibrary[item.type] || [];
        var index = list.findIndex(function (candidate) {
            return candidate.filename === item.filename;
        });
        return index >= 0 ? list[index + 1] || null : null;
    }

    function removeMediaFromLibrary(item) {
        var deletingCurrent = Boolean(currentMedia)
            && currentMedia.filename === item.filename;
        var replacement = deletingCurrent ? nextMediaAfter(item) : null;

        if (deletingCurrent) destroyPlayer();
        writeStorage(progressKey(item), null);
        mediaLibrary[item.type] = (mediaLibrary[item.type] || []).filter(function (candidate) {
            return candidate.filename !== item.filename;
        });

        if (!deletingCurrent) {
            renderPlaylist();
            return;
        }

        currentMedia = null;
        clearMediaLocation();
        if (replacement) playMedia(replacement, { autoplay: false });
        else {
            renderNowPlaying(null);
            renderPlaylist();
        }
    }

    async function deleteMedia() {
        var item = mediaDeleteTarget;
        if (!item || mediaDeleteBusy) return;
        mediaDeleteBusy = true;
        mediaDeleteCancel.disabled = true;
        mediaDeleteConfirm.disabled = true;
        mediaDeleteConfirm.textContent = '正在删除…';
        setMediaDeleteError('');
        try {
            var response = await fetch('/api/media_delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filename: item.filename })
            });
            var data = {};
            try {
                data = await response.json();
            } catch (error) {
                data = {};
            }
            if (!response.ok || !data.success) {
                setMediaDeleteError(data.msg || '删除失败，请稍后重试');
                return;
            }
            removeMediaFromLibrary(item);
            closeMediaDelete(true);
            showLibraryMessage('已永久删除“' + (item.title || item.filename) + '”');
        } catch (error) {
            setMediaDeleteError('网络异常，文件未删除');
        } finally {
            mediaDeleteBusy = false;
            if (!mediaDeleteOverlay.hidden) {
                mediaDeleteCancel.disabled = false;
                mediaDeleteConfirm.disabled = false;
                mediaDeleteConfirm.textContent = '永久删除';
            }
        }
    }

    function renderPlaylist() {
        if (!mediaLibrary) return;
        var items = mediaLibrary[libraryTab] || [];

        tabButtons.forEach(function (button) {
            button.setAttribute(
                'aria-selected',
                String(button.dataset.tab === libraryTab)
            );
        });
        stageCount.textContent = items.length + ' 个媒体';

        playlistItems.replaceChildren.apply(playlistItems, items.map(function (item) {
            var li = document.createElement('li');
            li.className = 'dl-playlist-row';
            var button = document.createElement('button');
            button.type = 'button';
            button.className = 'dl-playlist-item';
            button.dataset.type = item.type;
            button.dataset.filename = item.filename;
            button.setAttribute(
                'aria-current',
                String(Boolean(currentMedia) && currentMedia.filename === item.filename)
            );
            li.classList.toggle(
                'is-current',
                Boolean(currentMedia) && currentMedia.filename === item.filename
            );

            var cover = createPlaylistCover(item);

            var body = document.createElement('div');
            body.className = 'dl-playlist-item-body';
            var title = document.createElement('p');
            title.className = 'dl-playlist-item-title';
            title.textContent = item.title;
            title.title = item.title;
            var meta = document.createElement('p');
            meta.className = 'dl-playlist-item-meta';
            meta.textContent = mediaMetaLine(item);
            body.append(title, meta);

            button.append(cover, body);
            if (currentMedia && currentMedia.filename === item.filename) {
                button.appendChild(icon('#i-play', 'dl-playlist-item-playing'));
            }
            var deleteButton = document.createElement('button');
            deleteButton.type = 'button';
            deleteButton.className = 'dl-playlist-delete';
            deleteButton.dataset.filename = item.filename;
            deleteButton.setAttribute('aria-label', '删除“' + (item.title || item.filename) + '”');
            deleteButton.title = '删除文件';
            deleteButton.appendChild(icon('#i-trash'));
            li.append(button, deleteButton);
            return li;
        }));

        playlistEmpty.hidden = items.length > 0;
        if (!items.length) {
            playlistEmpty.textContent = libraryTab === 'audio'
                ? '还没有已下载的音频。'
                : '还没有已下载的视频。';
        }
    }

    function revealCurrentPlaylistItem() {
        // 只滚动播放列表容器：scrollIntoView 会连带滚动页面，自动播放下一条时会把页面拽走。
        var row = playlistItems.querySelector('.dl-playlist-row.is-current');
        if (!row || !playlistScroll || playlistScroll.clientHeight === 0) return;
        var rowRect = row.getBoundingClientRect();
        var scrollRect = playlistScroll.getBoundingClientRect();
        var style = window.getComputedStyle(playlistScroll);
        var top = scrollRect.top + (parseFloat(style.paddingTop) || 0);
        var bottom = scrollRect.bottom - (parseFloat(style.paddingBottom) || 0);
        if (rowRect.top < top) playlistScroll.scrollTop -= top - rowRect.top;
        else if (rowRect.bottom > bottom) playlistScroll.scrollTop += rowRect.bottom - bottom;
    }

    async function loadLibrary(preferredFile) {
        if (mediaLibrary || libraryLoading) {
            if (mediaLibrary) selectInitialMedia(preferredFile);
            return;
        }
        libraryLoading = true;
        try {
            var response = await fetch('/api/media_list');
            if (!response.ok) throw new Error('HTTP ' + response.status);
            var data = await response.json();
            if (!data.success) throw new Error(data.msg || '读取媒体列表失败');
            mediaLibrary = { video: data.video || [], audio: data.audio || [] };
            selectInitialMedia(preferredFile);
        } catch (error) {
            console.error('读取媒体列表失败:', error);
            playlistEmpty.hidden = false;
            playlistEmpty.textContent = '媒体列表读取失败，请稍后重试。';
        } finally {
            libraryLoading = false;
        }
    }

    function findMedia(filename) {
        if (!filename || !mediaLibrary) return null;
        var found = null;
        ['video', 'audio'].forEach(function (type) {
            if (found) return;
            found = (mediaLibrary[type] || []).find(function (item) {
                return item.filename === filename;
            }) || null;
            if (found) libraryTab = type;
        });
        return found;
    }

    function selectInitialMedia(preferredFile) {
        // 任务完成后的直达链接优先，其次才是当前标签的第一条。
        var target = findMedia(preferredFile);
        // 通过 file 参数点进来说明就是想看这一条，直接开播；否则只选中不播放。
        var openedByLink = Boolean(target);
        if (!target) {
            var items = mediaLibrary[libraryTab] || [];
            target = items[0] || null;
        }
        renderPlaylist();
        if (target) playMedia(target, { autoplay: openedByLink });
        else renderNowPlaying(null);
    }

    function setView(view, preferredFile) {
        var next = view === 'library' ? 'library' : 'download';
        panels.download.hidden = next !== 'download';
        panels.library.hidden = next !== 'library';
        modeLinks.forEach(function (link) {
            if (link.dataset.view === next) link.setAttribute('aria-current', 'page');
            else link.removeAttribute('aria-current');
        });
        if (next === 'library') loadLibrary(preferredFile);
        else destroyPlayer();
        return next;
    }

    modeLinks.forEach(function (link) {
        link.addEventListener('click', function (event) {
            event.preventDefault();
            var view = setView(link.dataset.view);
            var target = view === 'library' ? '/?view=library' : '/';
            window.history.replaceState({}, '', target);
        });
    });

    tabButtons.forEach(function (button) {
        button.addEventListener('click', function () {
            if (libraryTab === button.dataset.tab) return;
            libraryTab = button.dataset.tab;
            renderPlaylist();
            // 两个标签共用一个滚动容器，切换后不能停在上一个标签的位置。
            playlistScroll.scrollTop = 0;
            revealCurrentPlaylistItem();
        });
    });

    playlistItems.addEventListener('click', function (event) {
        var deleteButton = event.target.closest('.dl-playlist-delete');
        if (deleteButton && mediaLibrary) {
            var deleteItem = (mediaLibrary[libraryTab] || []).find(function (candidate) {
                return candidate.filename === deleteButton.dataset.filename;
            });
            if (deleteItem) openMediaDelete(deleteItem, deleteButton);
            return;
        }
        var button = event.target.closest('.dl-playlist-item');
        if (!button || !mediaLibrary) return;
        var item = (mediaLibrary[libraryTab] || []).find(function (candidate) {
            return candidate.filename === button.dataset.filename;
        });
        // 点击是用户手势，直接开播；复用媒体元素也让后续自动下一个获得授权。
        if (item) playMedia(item, { autoplay: true });
    });

    mediaDeleteCancel.addEventListener('click', function () {
        closeMediaDelete(false);
    });
    mediaDeleteConfirm.addEventListener('click', deleteMedia);
    mediaDeleteOverlay.addEventListener('click', function (event) {
        if (event.target === mediaDeleteOverlay) closeMediaDelete(false);
    });
    document.addEventListener('keydown', function (event) {
        if (mediaDeleteOverlay.hidden) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            closeMediaDelete(false);
            return;
        }
        if (event.key !== 'Tab') return;
        var controls = Array.prototype.filter.call(
            mediaDeleteDialog.querySelectorAll('button'),
            function (button) { return !button.disabled; }
        );
        if (!controls.length) return;
        var first = controls[0];
        var last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });

    /* 表单 */

    function selectedTypes() {
        return typeBoxes.filter(function (box) {
            return box.checked;
        }).map(function (box) {
            return box.value;
        });
    }

    function syncSubmitState() {
        submitButton.disabled = !urlInput.value.trim() || selectedTypes().length === 0;
    }

    function showError(message) {
        var element = heroSection.querySelector('.dl-error');
        if (!message) {
            if (element) element.remove();
            return;
        }
        if (!element) {
            element = document.createElement('div');
            element.className = 'dl-error';
            element.setAttribute('role', 'alert');
            form.after(element);
        }
        element.textContent = message;
    }

    form.addEventListener('submit', async function (event) {
        event.preventDefault();
        var url = urlInput.value.trim();
        var types = selectedTypes();
        if (!url || !types.length) return;

        try {
            window.localStorage.setItem(TYPES_KEY, JSON.stringify(types));
        } catch (error) {
            /* 忽略存储失败 */
        }

        submitButton.disabled = true;
        submitButton.replaceChildren(
            icon('#i-loader', 'dl-spin'),
            document.createTextNode(' 正在创建任务…')
        );
        showError('');

        try {
            var response = await fetch('/api/add_task', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url: url, types: types })
            });
            var data = await response.json();
            if (response.status === 401 && data.login_required) {
                // 匿名额度已用完；服务端记住提交，登录后会自动入队。
                window.location.href = data.login_url || loginUrl;
                return;
            }
            if (!response.ok || !data.success) {
                throw new Error(data.msg || '创建任务失败，请稍后重试');
            }
            addTasks(data.tasks || [], url);
            urlInput.value = '';
            render();
            scheduleTaskPoll();
            pollTasks();
        } catch (error) {
            showError(error.message || '创建任务失败，请稍后重试');
        } finally {
            submitButton.replaceChildren(
                icon('#i-plus'),
                document.createTextNode(' 加入队列')
            );
            syncSubmitState();
        }
    });

    urlInput.addEventListener('input', syncSubmitState);
    typeBoxes.forEach(function (box) {
        box.addEventListener('change', syncSubmitState);
    });

    /* 初始化 */

    function restoreTypes() {
        var saved;
        try {
            saved = JSON.parse(window.localStorage.getItem(TYPES_KEY) || 'null');
        } catch (error) {
            saved = null;
        }
        if (!Array.isArray(saved)) return;
        typeBoxes.forEach(function (box) {
            box.checked = saved.indexOf(box.value) !== -1;
        });
    }

    function bootstrap() {
        var payload = {
            tasks: [], url: '', view: 'download', tab: 'video', file: '',
            signedIn: false, loginUrl: '/login'
        };
        var node = document.getElementById('dl-bootstrap');
        if (node) {
            try {
                payload = JSON.parse(node.textContent) || payload;
            } catch (error) {
                /* 保持默认值 */
            }
        }

        tasks = readStore();
        // 服务端重定向带来的任务（无 JS 提交路径）合并进本地列表。
        addTasks(payload.tasks || [], payload.url || '');

        if (!typeBoxes.some(function (box) {
            return box.checked;
        })) {
            restoreTypes();
        }
        if (!typeBoxes.some(function (box) {
            return box.checked;
        })) {
            typeBoxes[0].checked = true;
        }

        // 登录态只影响账号 UI；匿名会话同样可以轮询任务和读取媒体库。
        signedIn = Boolean(payload.signedIn);
        loginUrl = payload.loginUrl || '/login';
        if (payload.tab === 'audio') libraryTab = 'audio';

        syncSubmitState();
        render();
        if (tasks.length) pollTasks();
        setView(payload.view, payload.file);
    }

    bootstrap();
})();
