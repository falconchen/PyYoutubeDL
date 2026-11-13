// Run with node --test tests/test_media_library.js.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/media-library.js'), 'utf8');

function setup(videojs) {
    const elements = {};
    const tabs = ['video', 'audio'].map(media => {
        elements[media + '-panel'] = { hidden: media !== 'video' };
        elements[media + '-list'] = { hidden: media !== 'video' };
        const attrs = { 'aria-selected': String(media === 'video') };
        return {
            dataset: { media }, attrs, events: {}, tabIndex: media === 'video' ? 0 : -1,
            setAttribute(name, value) { attrs[name] = value; },
            getAttribute() { return media + '-panel'; },
            addEventListener(name, callback) { this.events[name] = callback; },
            focus() { this.focused = true; }
        };
    });
    const window = {
        location: { href: 'https://example.com/player?file=video.mp4' },
        history: { replaceState(state, title, url) { window.location.href = String(url); } },
        dispatchEvent() {},
        updateActiveMediaDownloadButton(media) { this.downloadMedia = media; },
        videojs
    };
    const document = {
        querySelectorAll() { return tabs; },
        getElementById(id) { return elements[id]; },
        querySelector(selector) {
            if (selector === '.media-tabs') return { dataset: { playerUrl: '/player' } };
            const media = selector.includes('#audio-list') ? 'audio' : 'video';
            return { dataset: { filename: media + (media === 'audio' ? '.mp3' : '.mp4') } };
        }
    };
    vm.runInNewContext(source, { document, window, videojs, URL, Event });
    return { tabs, elements, window };
}

function assertSelected(state, media) {
    for (const tab of state.tabs) {
        const selected = tab.dataset.media === media;
        assert.equal(tab.attrs['aria-selected'], String(selected));
        assert.equal(tab.tabIndex, selected ? 0 : -1);
        assert.equal(state.elements[tab.dataset.media + '-panel'].hidden, !selected);
        assert.equal(state.elements[tab.dataset.media + '-list'].hidden, !selected);
    }
    const url = new URL(state.window.location.href);
    assert.equal(url.searchParams.get('tab'), media);
    assert.equal(url.searchParams.get('file'), media + (media === 'audio' ? '.mp3' : '.mp4'));
    assert.equal(state.window.downloadMedia, media);
}

test('lists remain usable when Video.js is unavailable or a player is uninitialized', () => {
    for (const videojs of [undefined, {}, { getPlayer() { return undefined; } }]) {
        const state = setup(videojs);
        assert.doesNotThrow(() => state.tabs[1].events.click());
        assertSelected(state, 'audio');
        assert.doesNotThrow(() => state.tabs[0].events.click());
        assertSelected(state, 'video');
    }
});

test('switching pauses the inactive player and supports keyboard navigation', () => {
    const paused = [];
    const state = setup({ getPlayer(id) { return { pause() { paused.push(id); } }; } });
    state.tabs[1].events.click();
    assertSelected(state, 'audio');
    assert.deepEqual(paused, ['video-player']);
    let prevented = false;
    state.tabs[1].events.keydown({ key: 'ArrowLeft', preventDefault() { prevented = true; } });
    assertSelected(state, 'video');
    assert.equal(prevented, true);
    assert.equal(state.tabs[0].focused, true);
    assert.deepEqual(paused, ['video-player', 'audio-player']);
});
