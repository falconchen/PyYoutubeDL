// Run with node --test tests/test_background_playback.js.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/background-playback.js'), 'utf8');

function setup(navigator = {}) {
    const context = { navigator, window: {}, Set, MediaMetadata: class { constructor(data) { Object.assign(this, data); } } };
    context.window.MediaMetadata = context.MediaMetadata;
    vm.runInNewContext(source, context);
    return context.window.BackgroundPlayback;
}
function player() {
    const events = {};
    let time = 20;
    return {
        paused: true,
        on(event, callback) { (events[event] ||= []).push(callback); },
        emit(event) { (events[event] || []).forEach(callback => callback()); },
        play() { this.paused = false; this.emit('play'); return Promise.resolve(); },
        pause() { this.paused = true; this.emit('pause'); },
        duration: () => 100, playbackRate: () => 1.5, poster: () => '',
        currentTime(value) { if (value !== undefined) time = value; return time; }
    };
}

test('system controls follow the active player and clamp seeking', () => {
    const handlers = {};
    const session = { setActionHandler(name, handler) { handlers[name] = handler; },
        setPositionState(state) { this.position = state; } };
    const nav = { mediaSession: session, audioSession: {} };
    const api = setup(nav);
    const video = player(), audio = player();
    api.attach(video, () => ({ title: '视频' }));
    api.attach(audio, () => ({ title: '歌曲' }));
    video.play();
    assert.equal(nav.audioSession.type, 'playback');
    assert.equal(session.metadata.title, '视频');
    audio.play();
    assert.equal(video.paused, true);
    assert.equal(session.metadata.title, '歌曲');
    video.emit('pause');
    video.emit('loadedmetadata');
    assert.equal(session.playbackState, 'playing');
    assert.equal(session.metadata.title, '歌曲');
    handlers.seekforward({ seekOffset: 500 });
    assert.equal(audio.currentTime(), 100);
    handlers.seekto({ seekTime: -5 });
    assert.equal(audio.currentTime(), 0);
    assert.equal(session.position.playbackRate, 1.5);
    handlers.pause();
    assert.equal(session.playbackState, 'paused');
    handlers.play();
    assert.equal(audio.paused, false);
    audio.emit('dispose');
    assert.equal(session.metadata, null);
    assert.equal(handlers.play, null);
});

test('missing or partially implemented APIs do not prevent playback', () => {
    for (const nav of [{}, { mediaSession: {
        setActionHandler() { throw Error('unsupported'); },
        setPositionState() { throw Error('unsupported'); }
    } }]) {
        const api = setup(nav), media = player();
        api.attach(media, () => ({}));
        assert.doesNotThrow(() => media.play());
        assert.equal(media.paused, false);
    }
});

test('iOS detection includes iPad desktop user agent', () => {
    assert.equal(setup({ userAgent: 'iPhone' }).isIOS, true);
    assert.equal(setup({ userAgent: 'Macintosh', platform: 'MacIntel', maxTouchPoints: 5 }).isIOS, true);
    assert.equal(setup({ userAgent: 'Macintosh', platform: 'MacIntel', maxTouchPoints: 0 }).isIOS, false);
});
