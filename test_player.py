import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as app_module
import ai_summary_store
from app import app


class TestPlayerPage(unittest.TestCase):
    def setUp(self):
        self.summary_temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.summary_temp_dir.cleanup)
        self.summary_db_path = str(
            Path(self.summary_temp_dir.name) / 'ai-summaries.sqlite3'
        )
        self.config_patcher = patch.dict(
            app_module.config,
            {'AI_SUMMARY_DB_PATH': self.summary_db_path},
        )
        self.config_patcher.start()
        self.addCleanup(self.config_patcher.stop)
        ai_summary_store.init_db(self.summary_db_path)
        self.client = app.test_client()
        app.testing = True
        app_module._probe_media_metadata.cache_clear()
        app_module._probe_media_source_url.cache_clear()
        self.source_url_patcher = patch('app.get_media_source_url', return_value='')
        self.source_url_patcher.start()
        self.addCleanup(self.source_url_patcher.stop)

    def test_player_shows_and_switches_original_source_url(self):
        source_url = 'https://www.youtube.com/watch?v=Hh3AmV46epI'
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'source.mp4').touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
                patch('app.get_media_source_url', return_value=source_url),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'href="{source_url}"', html)
        self.assertIn('>原始链接 <i', html)
        self.assertIn('id="current-video-source"', html)
        self.assertIn('var videoMetadata = {"source.mp4":', html)
        self.assertIn('updateCurrentVideoInfo(filename);', html)
        self.assertIn('link.href = sourceUrl;', html)

    def test_video_source_url_probe_reads_purl_or_comment_tags(self):
        source_url = 'https://example.com/original-video'
        probe_result = subprocess.CompletedProcess(
            args=['ffprobe'],
            returncode=0,
            stdout=(
                '{"format":{"tags":{"comment":"'
                + source_url
                + '"}}}'
            ),
            stderr='',
        )
        app_module._probe_media_source_url.cache_clear()

        with patch('app.subprocess.run', return_value=probe_result) as run:
            result = app_module._probe_media_source_url('/tmp/video.mp4', 1, 2)

        self.assertEqual(result, source_url)
        command = run.call_args.args[0]
        self.assertTrue(any(
            'title,artist,album,date,genre,description,synopsis,purl,comment'
            in argument
            for argument in command
        ))

    def test_video_metadata_is_displayed_with_cover_and_top_navigation(self):
        source_url = 'https://www.youtube.com/watch?v=Hh3AmV46epI'
        metadata = {
            'title': '测试视频',
            'artist': '测试作者',
            'album': '',
            'date': '2026-08-17',
            'genre': '科技',
            'description': '第一行\n第二行',
            'source_url': source_url,
        }
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'source.mp4').touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
                patch('app._probe_media_metadata', return_value=metadata),
                patch('app.get_media_source_url', return_value=source_url),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertIn('正在播放: 测试视频', html)
        self.assertIn('<dt>作者</dt>', html)
        self.assertIn('<dd>测试作者</dd>', html)
        self.assertIn('<summary>简介</summary>', html)
        self.assertIn('poster="https://i.ytimg.com/vi/Hh3AmV46epI/maxresdefault.jpg"', html)
        self.assertIn('updateVideoPoster(filename);', html)
        self.assertIn('href="/audio-player"', html)
        self.assertLess(html.index('class="player-nav"'), html.index('class="player-content"'))
        self.assertNotIn('class="footer-actions"', html)

    def test_player_header_aligns_title_and_navigation(self):
        template = Path(app.template_folder, 'player.html').read_text(
            encoding='utf-8',
        )
        audio_template = Path(app.template_folder, 'audio_player.html').read_text(
            encoding='utf-8',
        )
        css = Path(app.static_folder, 'player.css').read_text(encoding='utf-8')

        for page_template in (template, audio_template):
            self.assertIn('class="player-header-row"', page_template)
            self.assertLess(
                page_template.index('class="player-header-row"'),
                page_template.index('class="player-nav"'),
            )
        self.assertIn('align-items: center;', css)
        self.assertIn('margin-top: 0;', css)
        self.assertIn('border-radius: 999px;', css)
        self.assertIn('background: #fff7f7;', css)

    def test_download_control_is_rendered_for_current_video(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = 'test video.mp4'
            Path(files_dir, filename).touch()

            with patch('app.FILES_DIR', files_dir):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("videojs.registerComponent('DownloadButton'", html)
        self.assertIn("controlBar.addChild('DownloadButton'", html)
        self.assertIn("this.addClass('vjs-download-control')", html)
        self.assertIn('var currentFilename = "test video.mp4";', html)
        self.assertIn('link.download = currentFilename;', html)
        self.assertLess(html.index('video-js.min.css'), html.index('player.css'))

    def test_download_icon_uses_centered_control_box(self):
        css = Path(app.static_folder, 'player.css').read_text(encoding='utf-8')

        self.assertIn('cursor: pointer;', css)
        self.assertIn('align-items: center;', css)
        self.assertIn('justify-content: center;', css)
        self.assertIn('font-size: 1.3em;', css)
        self.assertIn('line-height: 1;', css)

    def test_video_player_uses_centered_theme_play_button(self):
        css = Path(app.static_folder, 'player.css').read_text(encoding='utf-8')

        self.assertIn('.player-page .video-js .vjs-big-play-button {', css)
        self.assertIn('top: 50%;', css)
        self.assertIn('left: 50%;', css)
        self.assertIn('border-radius: 50%;', css)
        self.assertIn('background: rgba(220, 20, 60, 0.84);', css)
        self.assertIn('.player-page .video-js.vjs-paused .vjs-big-play-button', css)
        self.assertIn('.player-page .video-js.vjs-playing .vjs-big-play-button', css)

    def test_video_player_uses_inline_playback_on_ios(self):
        template = Path(app.template_folder, 'player.html').read_text(
            encoding='utf-8',
        )

        self.assertIn('playsinline webkit-playsinline', template)
        self.assertIn("var player = videojs('video-player', {", template)
        self.assertIn('playsinline: true', template)
        self.assertNotIn('fullscreenToggle: false', template)

    def test_video_player_supports_playback_rates(self):
        template = Path(app.template_folder, 'player.html').read_text(
            encoding='utf-8',
        )

        self.assertIn(
            'playbackRates: [0.5, 0.75, 1, 1.5, 2, 3]',
            template,
        )

    def test_video_play_only_scrolls_inside_playlist(self):
        template = Path(app.template_folder, 'player.html').read_text(
            encoding='utf-8',
        )
        scroll_body = template.split('function scrollToActive()', 1)[1].split(
            "player.on('play', scrollToActive);",
            1,
        )[0]

        self.assertIn("document.getElementById('video-list')", scroll_body)
        self.assertIn('playlist.scrollTop += scrollDelta;', scroll_body)
        self.assertNotIn('scrollIntoView(', scroll_body)

    def test_player_layout_prevents_mobile_horizontal_overflow(self):
        css = Path(app.static_folder, 'player.css').read_text(encoding='utf-8')

        self.assertIn(
            'grid-template-columns: minmax(0, 1fr) minmax(280px, 350px);',
            css,
        )
        self.assertIn('.video-wrapper .video-js {', css)
        self.assertIn('width: calc(100% + 2rem);', css)
        self.assertGreaterEqual(css.count('min-width: 0;'), 4)

    def test_playlist_items_are_keyboard_accessible_buttons(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'first.mp4').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('type="button" class="playlist-item active"', html)
        self.assertIn('aria-current="true"', html)
        self.assertIn("element.setAttribute('aria-current', 'true');", html)

    def test_file_parameter_selects_requested_video(self):
        with tempfile.TemporaryDirectory() as files_dir:
            first = Path(files_dir, 'first.mp4')
            requested = Path(files_dir, 'requested video.mp4')
            first.touch()
            requested.touch()
            first.touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
            ):
                response = self.client.get(
                    '/player',
                    query_string={'file': requested.name},
                )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'var currentFilename = "requested video.mp4";',
            html,
        )
        self.assertIn(
            '正在播放: requested video.mp4',
            html,
        )

    def test_file_parameter_ignores_unknown_and_non_mp4_paths(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = 'available.mp4'
            Path(files_dir, filename).touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
            ):
                response = self.client.get(
                    '/player',
                    query_string={'file': '../config.json'},
                )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('var currentFilename = "available.mp4";', html)

    def test_switch_video_updates_file_parameter_in_url(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'first.mp4').touch()
            Path(files_dir, 'second video.mp4').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("url.searchParams.set('file', filename);", html)
        self.assertIn(
            "window.history.replaceState({ filename: filename }, '', url);",
            html,
        )
        self.assertIn('updatePlayerUrl(filename);', html)
        self.assertLess(
            html.index('currentFilename = filename;'),
            html.index('updatePlayerUrl(filename);'),
        )

    def test_player_saves_and_restores_progress_per_video(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, 'first.mp4').touch()
            Path(files_dir, 'second.mp4').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "var playbackProgressPrefix = 'pyyoutubedl:playback-progress:';",
            html,
        )
        self.assertIn(
            'localStorage.setItem(\n'
            '                    playbackProgressKey(currentFilename),',
            html,
        )
        self.assertIn("player.on('loadedmetadata', function () {", html)
        loaded_metadata_start = html.index(
            "player.on('loadedmetadata', function () {"
        )
        self.assertIn(
            'restoreCurrentVideoProgress();',
            html[loaded_metadata_start:],
        )
        self.assertIn("player.on('timeupdate', function () {", html)
        self.assertIn(
            "player.on('pause', saveCurrentVideoProgress);",
            html,
        )
        self.assertIn(
            "window.addEventListener('beforeunload', saveCurrentVideoProgress);",
            html,
        )
        self.assertIn('clearVideoProgress(currentFilename);', html)
        self.assertIn(
            'switchVideo(nextSrc, nextFile, items[currentIndex], false);',
            html,
        )
        switch_start = html.index(
            'function switchVideo(src, filename, element, savePrevious)'
        )
        source_update = html.index(
            'player.src({ type: "video/mp4", src: src });',
            switch_start,
        )
        self.assertLess(
            html.index('saveCurrentVideoProgress();', switch_start),
            source_update,
        )

    def test_player_filters_filenames_by_configured_keywords(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, '保留的视频.mp4').touch()
            Path(files_dir, '包含预告的预告片.mp4').touch()
            Path(files_dir, 'sample-preview.mp4').touch()
            Path(files_dir, '不是视频.mp3').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch.dict(
                    app_module.config,
                    {'PLAYER_FILENAME_EXCLUDE_KEYWORDS': ['预告', 'preview', '']},
                ),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('保留的视频.mp4', html)
        self.assertNotIn('包含预告的预告片.mp4', html)
        self.assertNotIn('sample-preview.mp4', html)

    def test_player_ignores_invalid_keyword_config(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, '正常视频.mp4').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch.dict(
                    app_module.config,
                    {'PLAYER_FILENAME_EXCLUDE_KEYWORDS': '不是数组'},
                ),
            ):
                response = self.client.get('/player')

        self.assertEqual(response.status_code, 200)
        self.assertIn('正常视频.mp4', response.get_data(as_text=True))

    def test_player_loads_embedded_subtitles_as_webvtt_tracks(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = '带字幕.mp4'
            Path(files_dir, filename).touch()
            subtitles = [
                {'stream_index': 2, 'language': 'zh-Hans', 'label': '简体中文'},
                {'stream_index': 3, 'language': 'zh-Hant', 'label': '繁体中文'},
            ]

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=subtitles),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('kind="subtitles"', html)
        self.assertIn('srclang="zh-Hans" label="简体中文"', html)
        self.assertIn('srclang="zh-Hant" label="繁体中文"', html)
        self.assertIn('/subtitles/', html)
        self.assertIn('player.addRemoteTextTrack({', html)
        self.assertIn('player.removeRemoteTextTrack(currentTracks[index]);', html)

    def test_player_uses_accept_language_for_default_subtitle(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = '带字幕.mp4'
            Path(files_dir, filename).touch()
            subtitles = [
                {'stream_index': 2, 'language': 'zh-Hans', 'label': '简体中文'},
                {'stream_index': 3, 'language': 'en', 'label': 'English'},
            ]

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=subtitles),
            ):
                response = self.client.get(
                    '/player',
                    headers={
                        'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,fr;q=0',
                    },
                )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'var browser_subtitle_languages = ["en-US", "en", "zh-CN"]',
            html,
        )
        self.assertIn("normalizedLanguages.unshift('zh-hans');", html)
        self.assertIn('subtitleLanguageVariant(language)', html)

    def test_player_persists_manual_subtitle_language_and_off_state(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, '带字幕.mp4').touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch(
                    'app.get_embedded_subtitles',
                    return_value=[
                        {'stream_index': 2, 'language': 'en', 'label': 'English'},
                    ],
                ),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "var subtitlePreferenceKey = 'pyyoutubedl:subtitle-preference';",
            html,
        )
        self.assertIn(
            "player.textTracks().addEventListener('change', saveSubtitlePreference);",
            html,
        )
        self.assertIn("mode: 'language'", html)
        self.assertIn("writeSubtitlePreference({ mode: 'off' });", html)
        self.assertIn("savedPreference.mode !== 'off'", html)
        self.assertIn('applySubtitlePreference();', html)
        preference_start = html.index('function saveSubtitlePreference()')
        self.assertIn(
            'updateAiSummaryPanel(currentFilename);',
            html[preference_start:],
        )

    def test_player_renders_on_demand_ai_summary_controls(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, '带字幕.mp4').touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch(
                    'app.get_embedded_subtitles',
                    return_value=[
                        {'stream_index': 2, 'language': 'zh-Hans', 'label': '简体中文'},
                    ],
                ),
                patch.dict(
                    app_module.config,
                    {
                        'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
                        'AI_API_MODEL': 'test-model',
                        'AI_API_TOKEN': 'test-token',
                    },
                ),
            ):
                response = self.client.get('/player')

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="generate-ai-summary"', html)
        self.assertIn('var aiSummaryConfigured = true;', html)
        self.assertIn("fetch(\"/api/ai_summary\"", html)
        self.assertIn('stream_index: track.stream_index', html)
        self.assertNotIn('test-token', html)

    def test_ai_summary_renders_sanitized_markdown_as_html(self):
        template = Path(app.template_folder, 'player.html').read_text(
            encoding='utf-8',
        )
        css = Path(app.static_folder, 'player.css').read_text(encoding='utf-8')

        self.assertIn('marked@18.0.7/lib/marked.umd.js', template)
        self.assertIn('dompurify@3.4.12/dist/purify.min.js', template)
        self.assertIn('var renderedHtml = marked.parse(markdown', template)
        self.assertIn('var sanitizedHtml = DOMPurify.sanitize(renderedHtml', template)
        self.assertIn("FORBID_TAGS: ['img', 'svg', 'math', 'style']", template)
        self.assertIn('content.innerHTML = sanitizedHtml;', template)
        self.assertIn('content.textContent = markdown;', template)
        self.assertIn('.ai-summary-content h2 {', css)
        self.assertIn('.ai-summary-content pre code {', css)

    def test_ai_summary_has_copy_and_expand_controls(self):
        template = Path(app.template_folder, 'player.html').read_text(
            encoding='utf-8',
        )
        css = Path(app.static_folder, 'player.css').read_text(encoding='utf-8')

        self.assertIn('id="copy-ai-summary"', template)
        self.assertIn('id="toggle-ai-summary"', template)
        self.assertIn('navigator.clipboard.writeText(summary);', template)
        self.assertIn('fallbackCopyText(summary);', template)
        self.assertIn("toggleButton.textContent = expanded ? '收起' : '展开';", template)
        self.assertIn("toggleButton.setAttribute('aria-expanded', String(expanded));", template)
        self.assertIn("content.classList.toggle('is-collapsed', !streaming);", template)
        self.assertIn('consumeAiSummaryStream(result.job_id', template)
        self.assertIn('application/x-ndjson', template)
        self.assertIn('.ai-summary-content.is-collapsed {', css)

    def test_ai_summary_requires_server_configuration(self):
        with patch.dict(
            app_module.config,
            {'AI_API_BASE_URL': '', 'AI_API_MODEL': '', 'AI_API_TOKEN': ''},
        ):
            response = self.client.post(
                '/api/ai_summary',
                json={'filename': 'video.mp4', 'stream_index': 2},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()['message'], 'AI 总结尚未完成配置')

    def test_ai_summary_rejects_video_without_subtitles(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, '无字幕.mp4').touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
                patch.dict(
                    app_module.config,
                    {
                        'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
                        'AI_API_MODEL': 'test-model',
                        'AI_API_TOKEN': 'test-token',
                    },
                ),
            ):
                response = self.client.post(
                    '/api/ai_summary',
                    json={'filename': '无字幕.mp4'},
                )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['message'], '当前视频没有可用字幕')

    def test_ai_summary_creates_and_reuses_async_local_job(self):
        with tempfile.TemporaryDirectory() as files_dir:
            Path(files_dir, '带字幕.mp4').touch()
            with (
                patch('app.FILES_DIR', files_dir),
                patch(
                    'app.get_embedded_subtitles',
                    return_value=[
                        {'stream_index': 2, 'language': 'zh-Hans', 'label': '简体中文'},
                    ],
                ),
                patch.dict(
                    app_module.config,
                    {
                        'AI_API_BASE_URL': 'https://ai.example/v1/chat/completions',
                        'AI_API_MODEL': 'test-model',
                        'AI_API_TOKEN': 'test-token',
                    },
                ),
            ):
                first = self.client.post(
                    '/api/ai_summary',
                    json={'filename': '带字幕.mp4', 'stream_index': 2},
                )
                second = self.client.post(
                    '/api/ai_summary',
                    json={'filename': '带字幕.mp4', 'stream_index': 2},
                )

        self.assertEqual(first.status_code, 202)
        self.assertFalse(first.get_json()['cached'])
        self.assertEqual(first.get_json()['status'], 'queued')
        self.assertEqual(first.get_json()['job_id'], second.get_json()['job_id'])

    def test_probe_distinguishes_simplified_and_traditional_chinese_tracks(self):
        probe_result = subprocess.CompletedProcess(
            args=['ffprobe'],
            returncode=0,
            stdout='''{
                "streams": [
                    {"index": 2, "tags": {"language": "zho"}},
                    {"index": 3, "tags": {"language": "zho"}}
                ]
            }''',
            stderr='',
        )

        app_module._probe_embedded_subtitles.cache_clear()
        with patch('app.subprocess.run', return_value=probe_result):
            subtitles = app_module._probe_embedded_subtitles(
                '/tmp/video-with-chinese-subs.mp4',
                1,
                1,
            )

        self.assertEqual(
            subtitles,
            (
                {'stream_index': 2, 'language': 'zh-Hans', 'label': '简体中文'},
                {'stream_index': 3, 'language': 'zh-Hant', 'label': '繁体中文'},
            ),
        )

    def test_subtitle_route_converts_valid_stream_to_webvtt(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = '带字幕.mp4'
            Path(files_dir, filename).touch()
            completed = subprocess.CompletedProcess(
                args=['ffmpeg'],
                returncode=0,
                stdout='WEBVTT\n\n00:00.000 --> 00:01.000\n测试\n'.encode(),
                stderr=b'',
            )

            with (
                patch('app.FILES_DIR', files_dir),
                patch(
                    'app.get_embedded_subtitles',
                    return_value=[{'stream_index': 2, 'language': 'zh', 'label': '中文'}],
                ),
                patch('app.subprocess.run', return_value=completed) as run,
            ):
                response = self.client.get('/subtitles/%E5%B8%A6%E5%AD%97%E5%B9%95.mp4/2.vtt')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'text/vtt')
        self.assertTrue(response.data.startswith(b'WEBVTT'))
        self.assertIn('0:2', run.call_args.args[0])

    def test_subtitle_route_rejects_unknown_stream(self):
        with tempfile.TemporaryDirectory() as files_dir:
            filename = '带字幕.mp4'
            Path(files_dir, filename).touch()

            with (
                patch('app.FILES_DIR', files_dir),
                patch('app.get_embedded_subtitles', return_value=[]),
            ):
                response = self.client.get('/subtitles/%E5%B8%A6%E5%AD%97%E5%B9%95.mp4/99.vtt')

        self.assertEqual(response.status_code, 404)


if __name__ == '__main__':
    unittest.main()
