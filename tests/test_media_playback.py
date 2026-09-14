"""媒体库播放增强：复用播放器、倍速延续、进度记忆、自动下一个与 Media Session。

这些行为运行在浏览器里，这里沿用 test_task_info.py 的做法对 dropload.js 做
静态断言，锁定关键约定，避免被无意改回「每次换源都重建播放器」。
"""
import unittest
from pathlib import Path

from app import app


class TestMediaPlayback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = Path(app.static_folder, 'dropload.js').read_text(
            encoding='utf-8',
        )

    def test_player_is_reused_only_within_the_same_media_type(self):
        # iOS 上新建的媒体元素没有用户手势授权，自动下一个必须复用同一实例；
        # 但 zwplayer 的 play(url) 不切换标准/音乐模式，跨类型必须重建。
        self.assertIn(
            'if (zwplayer && autoplay && playerType === item.type) {',
            self.script,
        )
        self.assertIn('zwplayer.play(item.url);', self.script)
        self.assertIn('playerType = item.type;', self.script)
        self.assertIn('if (!element || element === mediaEl) return;', self.script)

    def test_file_link_autoplays_with_unmute_prompt_fallback(self):
        # 只有通过 file 参数定位到条目时才自动播放
        self.assertIn('var openedByLink = Boolean(target);', self.script)
        self.assertIn('if (target) playMedia(target, { autoplay: openedByLink });', self.script)
        # 被浏览器拦截时 zwplayer 改为静音播放，必须保留「点击打开声音」提示
        self.assertIn('disableMutedConfirm: false,', self.script)
        self.assertNotIn('disableMutedConfirm: true', self.script)

    def test_location_follows_the_current_item(self):
        self.assertIn('if (autoplay) updateLocationForMedia(item);', self.script)
        self.assertIn(
            "window.history.replaceState({}, '', '/player?' + params.toString());",
            self.script,
        )

    def test_zwplayer_music_mode_does_not_own_media_session(self):
        self.assertIn('music: { mediaSession: false },', self.script)

    def test_speed_button_is_enabled_and_rate_is_remembered(self):
        self.assertIn('speedButton: true,', self.script)
        self.assertIn("var PLAYBACK_RATE_KEY = 'dropload:playback-rate';", self.script)
        self.assertIn('var PLAYBACK_RATES = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2];', self.script)
        self.assertIn('mediaEl.defaultPlaybackRate = rate;', self.script)
        self.assertIn('zwplayer._syncSpeedBtnUI(mediaEl.playbackRate);', self.script)

    def test_playback_progress_is_saved_and_restored(self):
        self.assertIn(
            "var PROGRESS_KEY_PREFIX = 'dropload:playback-progress:';",
            self.script,
        )
        self.assertIn('var PLAYBACK_END_THRESHOLD = 3;', self.script)
        # 换源途中不写进度，避免把上一条的播放位置记到新条目上
        self.assertIn(
            'if (!mediaEl || !currentMedia || loadedFilename !== currentMedia.filename) return;',
            self.script,
        )
        self.assertIn("window.addEventListener('pagehide'", self.script)
        self.assertIn("document.visibilityState === 'hidden'", self.script)

    def test_ended_advances_to_next_item_of_the_same_type(self):
        self.assertIn('var list = mediaLibrary[currentMedia.type] || [];', self.script)
        self.assertIn('if (next) playMedia(next, { autoplay: true });', self.script)
        self.assertIn("else setSessionPlaybackState('none');", self.script)

    def test_media_session_handles_lock_screen_controls(self):
        for action in ['play', 'pause', 'seekbackward', 'seekforward', 'seekto',
                       'previoustrack', 'nexttrack']:
            with self.subTest(action=action):
                self.assertIn(f"setSessionAction('{action}'", self.script)
        self.assertIn("navigator.audioSession.type = 'playback';", self.script)


if __name__ == '__main__':
    unittest.main()
