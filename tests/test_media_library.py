"""媒体库：入口路由保留深链，列表由 /api/media_list 提供。"""
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app as app_module
import user_store
from auth_helper import logged_in_client, other_user


def bootstrap_payload(html):
    """取出模板写入的引导 JSON，前端据此决定初始视图。"""
    match = re.search(
        r'<script id="dl-bootstrap" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    assert match, '缺少 dl-bootstrap'
    return json.loads(match.group(1))


def test_library_routes_carry_view_tab_and_file():
    with logged_in_client() as (client, _user, _db):
        _assert_routes(client)


def _assert_routes(client):
    cases = [
        ('/', 'download', 'video', ''),
        ('/?view=library', 'library', 'video', ''),
        ('/?view=library&tab=audio', 'library', 'audio', ''),
        ('/player', 'library', 'video', ''),
        ('/player?tab=audio', 'library', 'audio', ''),
        ('/audio-player', 'library', 'audio', ''),
        ('/audio-player?file=audio.mp3', 'library', 'audio', 'audio.mp3'),
        ('/player?file=video.mp4', 'library', 'video', 'video.mp4'),
    ]
    for path, view, tab, requested in cases:
        response = client.get(path)
        assert response.status_code == 200, path
        payload = bootstrap_payload(response.get_data(as_text=True))
        assert payload['view'] == view, path
        assert payload['tab'] == tab, path
        assert payload['file'] == requested, path


def test_requested_file_wins_over_tab():
    """已完成任务的直达链接要能覆盖 tab，逻辑在 dropload.js 的 findMedia 中。"""
    script = Path(app_module.app.static_folder, 'dropload.js').read_text(
        encoding='utf-8',
    )
    assert 'function findMedia(filename)' in script
    assert 'if (found) libraryTab = type;' in script
    assert 'var target = findMedia(preferredFile);' in script


def test_media_list_splits_video_and_audio():
    with logged_in_client() as (client, _user, _db), TemporaryDirectory() as files_dir:
        for name in ['video.mp4', 'audio.mp3', "quote's video.mp4"]:
            Path(files_dir, name).touch()
        with patch('app.FILES_DIR', files_dir):
            response = client.get('/api/media_list')
            payload = response.get_json()

    assert response.status_code == 200
    assert payload['success'] is True
    assert {item['filename'] for item in payload['video']} == {
        'video.mp4',
        "quote's video.mp4",
    }
    assert [item['filename'] for item in payload['audio']] == ['audio.mp3']
    for item in payload['video'] + payload['audio']:
        assert item['url'].startswith('/files/')
        assert item['download_url'].startswith('/downloads/')
        assert item['title']
        assert item['thumbnail_candidates']
        assert item['thumbnail_candidates'][-1].endswith(
            '/static/images/media-cover-default.svg'
        )


def test_media_list_carries_source_link_and_description():
    video_metadata = {
        'title': '视频标题',
        'artist': '作者',
        'description': '第一行\n第二行',
        'source_url': 'https://www.youtube.com/watch?v=abc',
        'cover_candidates': [],
    }
    audio_metadata = dict(
        video_metadata,
        description='',
        source_url='https://www.bilibili.com/video/BV1',
    )
    with logged_in_client() as (client, _user, _db), TemporaryDirectory() as files_dir:
        for name in ['video.mp4', 'audio.mp3']:
            Path(files_dir, name).touch()
        with (
            patch('app.FILES_DIR', files_dir),
            patch('app.get_video_metadata', return_value=video_metadata),
            patch('app.get_audio_metadata', return_value=audio_metadata),
        ):
            payload = client.get('/api/media_list').get_json()

    video, audio = payload['video'][0], payload['audio'][0]
    assert video['source_url'] == 'https://www.youtube.com/watch?v=abc'
    assert video['description'] == '第一行\n第二行'
    assert audio['source_url'] == 'https://www.bilibili.com/video/BV1'
    assert audio['description'] == ''


def test_media_list_offers_sidecar_subtitles_for_mp4_videos():
    with logged_in_client() as (client, _user, _db), TemporaryDirectory() as files_dir:
        for name in [
            'clip.mp4', 'clip.en.srt', 'clip.zh-Hans.srt', 'plain.mp4'
        ]:
            Path(files_dir, name).write_text(
                '1\n00:00:01,000 --> 00:00:02,000\n你好\n', encoding='utf-8'
            )
        with (
            patch('app.FILES_DIR', files_dir),
            patch('app._probe_embedded_subtitles', return_value=()),
        ):
            payload = client.get('/api/media_list').get_json()
            videos = {item['filename']: item for item in payload['video']}
            subtitle_status = client.get(videos['clip.mp4']['subtitles'][0]['url']).status_code

    tracks = videos['clip.mp4']['subtitles']
    # 简体中文排在英文前面，前端把第一条作为默认主字幕
    assert [track['language'] for track in tracks] == ['zh-Hans', 'en']
    assert [track['label'] for track in tracks] == ['简体中文', 'English']
    assert tracks[0]['url'].startswith('/subtitles/sidecar/')
    assert subtitle_status == 200
    assert videos['plain.mp4']['subtitles'] == []


def test_regional_chinese_subtitle_codes_sort_before_english():
    with TemporaryDirectory() as files_dir:
        for name in ['clip.mp4', 'clip.en.srt', 'clip.zh-CN.srt']:
            Path(files_dir, name).touch()
        with patch('app.FILES_DIR', files_dir):
            tracks = app_module.get_sidecar_subtitles('clip.mp4')

    assert [(track['language'], track['label']) for track in tracks] == [
        ('zh-Hans', '简体中文'),
        ('en', 'English'),
    ]
    assert tracks[0]['subtitle_filename'] == 'clip.zh-CN.srt'


def test_media_list_applies_exclude_keywords():
    with logged_in_client() as (client, _user, _db), TemporaryDirectory() as files_dir:
        for name in ['keep.mp4', 'skip-me.mp4']:
            Path(files_dir, name).touch()
        with patch('app.FILES_DIR', files_dir), patch.dict(
            app_module.config,
            {'PLAYER_FILENAME_EXCLUDE_KEYWORDS': ['skip']},
        ):
            payload = client.get('/api/media_list').get_json()

    assert [item['filename'] for item in payload['video']] == ['keep.mp4']


def test_empty_library_returns_empty_lists():
    with logged_in_client() as (client, _user, _db), TemporaryDirectory() as files_dir:
        with patch('app.FILES_DIR', files_dir):
            payload = client.get('/api/media_list').get_json()

    assert payload['success'] is True
    assert payload['video'] == []
    assert payload['audio'] == []


def test_library_panel_has_accessible_tabs():
    with logged_in_client() as (client, _user, _db):
        html = client.get('/player').get_data(as_text=True)

    assert 'role="tablist"' in html
    assert 'data-tab="video"' in html
    assert 'data-tab="audio"' in html


def test_media_description_is_collapsed_by_default():
    with logged_in_client() as (client, _user, _db):
        html = client.get('/player').get_data(as_text=True)

    assert '<details class="dl-now-description" hidden>' in html
    script = Path(app_module.app.static_folder, 'dropload.js').read_text(
        encoding='utf-8',
    )
    assert 'nowDescription.open = false;' in script


def test_media_list_only_returns_own_files():
    """媒体私有：他人下载的文件不应出现在自己的媒体库里。"""
    with logged_in_client() as (client, user, db_path), TemporaryDirectory() as files_dir:
        Path(files_dir, 'mine.mp4').touch()
        Path(files_dir, 'theirs.mp4').touch()
        stranger = other_user(db_path)
        user_store.record_media(db_path, 'mine.mp4', user['id'])
        user_store.record_media(db_path, 'theirs.mp4', stranger['id'])

        with patch('app.FILES_DIR', files_dir):
            payload = client.get('/api/media_list').get_json()

    assert [item['filename'] for item in payload['video']] == ['mine.mp4']


def test_serving_another_users_file_is_not_found():
    with logged_in_client() as (client, _user, db_path), TemporaryDirectory() as files_dir:
        Path(files_dir, 'theirs.mp4').write_bytes(b'x')
        stranger = other_user(db_path)
        user_store.record_media(db_path, 'theirs.mp4', stranger['id'])

        with patch('app.FILES_DIR', files_dir):
            played = client.get('/files/theirs.mp4')
            downloaded = client.get('/downloads/theirs.mp4')

    # 用 404 而不是 403，避免泄露「该文件存在」
    assert played.status_code == 404
    assert downloaded.status_code == 404


def test_anonymous_can_open_pages_and_an_empty_private_library():
    """匿名页面和媒体接口开放，但只返回当前匿名会话的资源。"""
    app_module.app.testing = True
    client = app_module.app.test_client()

    assert client.get('/').status_code == 200
    assert client.get('/player').status_code == 200
    payload = client.get('/api/media_list').get_json()
    assert payload['success'] is True
    assert payload['video'] == []
    assert payload['audio'] == []


def test_delete_owned_media_removes_only_file_and_ownership():
    with logged_in_client() as (client, user, db_path), TemporaryDirectory() as files_dir:
        filename = 'delete-me.mp4'
        filepath = Path(files_dir, filename)
        filepath.write_bytes(b'media')
        user_store.record_tasks(
            db_path, ['v20260914000000Keep'], user['id']
        )
        user_store.record_media(
            db_path, filename, user['id'], 'v20260914000000Keep'
        )

        with patch('app.FILES_DIR', files_dir):
            response = client.post('/api/media_delete', json={'filename': filename})

        assert response.status_code == 200
        assert response.get_json() == {'success': True, 'filename': filename}
        assert not filepath.exists()
        assert user_store.media_owner(db_path, filename) is None
        assert user_store.task_owner(db_path, 'v20260914000000Keep') == user['id']


def test_delete_media_never_touches_another_owners_file():
    with logged_in_client() as (client, _user, db_path), TemporaryDirectory() as files_dir:
        filename = 'theirs.mp4'
        filepath = Path(files_dir, filename)
        filepath.write_bytes(b'media')
        stranger = other_user(db_path)
        user_store.record_media(db_path, filename, stranger['id'])

        with patch('app.FILES_DIR', files_dir):
            response = client.post('/api/media_delete', json={'filename': filename})

        assert response.status_code == 404
        assert filepath.exists()
        assert user_store.media_owner(db_path, filename) == stranger['id']


def test_anonymous_owner_can_delete_own_media():
    with TemporaryDirectory() as directory, TemporaryDirectory() as files_dir:
        db_path = str(Path(directory, 'users.sqlite3'))
        user_store.init_db(db_path)
        filename = 'anonymous.mp3'
        filepath = Path(files_dir, filename)
        filepath.write_bytes(b'audio')
        anonymous_id = 'anonymous-owner'
        user_store.record_anonymous_media(db_path, filename, anonymous_id)
        app_module.app.testing = True
        client = app_module.app.test_client()
        with client.session_transaction() as session:
            session[app_module.SESSION_ANONYMOUS_KEY] = anonymous_id

        with (
            patch('app.USER_DB_PATH', db_path),
            patch('app.FILES_DIR', files_dir),
        ):
            response = client.post('/api/media_delete', json={'filename': filename})

        assert response.status_code == 200
        assert not filepath.exists()
        assert user_store.anonymous_media_owner(db_path, filename) is None


def test_delete_media_rejects_invalid_or_missing_filename():
    with logged_in_client() as (client, _user, _db):
        for payload in (
            {},
            [],
            {'filename': ''},
            {'filename': '../video.mp4'},
            {'filename': r'folder\video.mp4'},
            {'filename': 123},
        ):
            response = client.post('/api/media_delete', json=payload)
            assert response.status_code == 400, payload


def test_delete_missing_owned_media_returns_not_found():
    with logged_in_client() as (client, user, db_path), TemporaryDirectory() as files_dir:
        user_store.record_media(db_path, 'missing.mp4', user['id'])
        with patch('app.FILES_DIR', files_dir):
            response = client.post(
                '/api/media_delete', json={'filename': 'missing.mp4'}
            )

    assert response.status_code == 404


def test_delete_media_filesystem_error_keeps_file_and_ownership():
    with logged_in_client() as (client, user, db_path), TemporaryDirectory() as files_dir:
        filename = 'busy.mp4'
        filepath = Path(files_dir, filename)
        filepath.write_bytes(b'media')
        user_store.record_media(db_path, filename, user['id'])

        with (
            patch('app.FILES_DIR', files_dir),
            patch('app.os.remove', side_effect=PermissionError('busy')),
        ):
            response = client.post('/api/media_delete', json={'filename': filename})

        assert response.status_code == 500
        assert response.get_json()['msg'] == '删除文件失败'
        assert filepath.exists()
        assert user_store.media_owner(db_path, filename) == user['id']


def test_sync_media_ownership_skips_result_files_that_no_longer_exist():
    with (
        logged_in_client() as (_client, user, db_path),
        TemporaryDirectory() as files_dir,
        TemporaryDirectory() as urls_dir,
    ):
        task_id = 'v20260914000000Gone'
        user_store.record_tasks(db_path, [task_id], user['id'])
        Path(urls_dir, f'{task_id}.result.json').write_text(
            '{"files":["gone.mp4"]}', encoding='utf-8'
        )

        with (
            patch('app.FILES_DIR', files_dir),
            patch('app.URLS_DIR', urls_dir),
        ):
            owners = app_module.sync_media_ownership()

        assert 'gone.mp4' not in owners
        assert user_store.media_owner(db_path, 'gone.mp4') is None


def test_library_delete_ui_has_separate_controls_and_confirmation():
    with logged_in_client() as (client, _user, _db):
        html = client.get('/player').get_data(as_text=True)
    script = Path(app_module.app.static_folder, 'dropload.js').read_text(
        encoding='utf-8',
    )
    stylesheet = Path(app_module.app.static_folder, 'dropload.css').read_text(
        encoding='utf-8',
    )

    assert 'class="dl-media-delete-dialog"' in html
    assert 'aria-modal="true"' in html
    assert "deleteButton.className = 'dl-playlist-delete';" in script
    assert "fetch('/api/media_delete'" in script
    assert "body: JSON.stringify({ filename: item.filename })" in script
    assert 'writeStorage(progressKey(item), null);' in script
    assert 'if (deletingCurrent) destroyPlayer();' in script
    assert 'if (replacement) playMedia(replacement, { autoplay: false });' in script
    assert '@media (min-width: 640px) and (hover: hover) and (pointer: fine)' in stylesheet
    assert '.dl-playlist-row:hover .dl-playlist-delete' in stylesheet
    assert 'opacity: 0;' in stylesheet
    assert '.dl-playlist-item-playing {' in stylesheet
    assert 'align-self: center;' in stylesheet


SRT_LYRICS = '1\n00:00:01,000 --> 00:00:02,500\n第一句\nFirst line\n\n2\n00:00:03,000 --> 00:00:04,000\n第二句\n'


def test_audio_lyrics_are_listed_and_served_as_lrc():
    with logged_in_client() as (client, _user, _db), TemporaryDirectory() as files_dir:
        Path(files_dir, 'song.mp3').write_bytes(b'x')
        Path(files_dir, 'song.en.srt').write_text('1\n00:00:09,000 --> 00:00:10,000\nEnglish\n', encoding='utf-8')
        Path(files_dir, 'song.zh-Hans.srt').write_text(SRT_LYRICS, encoding='utf-8')
        Path(files_dir, 'silent.mp3').write_bytes(b'x')
        with patch('app.FILES_DIR', files_dir):
            audios = {
                item['filename']: item
                for item in client.get('/api/media_list').get_json()['audio']
            }
            response = client.get(audios['song.mp3']['lyrics_url'])

    assert audios['silent.mp3']['lyrics_url'] == ''
    assert response.status_code == 200
    lyrics = response.get_data(as_text=True)
    # 简体中文优先于英文；多行字幕拆成同一时间戳的几行，zwplayer 显示为主歌词加翻译
    assert '[00:01.00]第一句' in lyrics
    assert '[00:01.00]First line' in lyrics
    assert '[00:03.00]第二句' in lyrics
    assert 'English' not in lyrics


def test_lrc_lyrics_are_served_unchanged():
    with logged_in_client() as (client, _user, _db), TemporaryDirectory() as files_dir:
        Path(files_dir, 'song.m4a').write_bytes(b'x')
        Path(files_dir, 'song.lrc').write_text('[00:05.00]原样歌词\n', encoding='utf-8')
        with patch('app.FILES_DIR', files_dir):
            response = client.get('/lyrics/song.m4a.lrc')

    assert response.status_code == 200
    assert response.get_data(as_text=True) == '[00:05.00]原样歌词\n'


def test_lyrics_follow_audio_access_rules():
    with logged_in_client() as (client, _user, db_path), TemporaryDirectory() as files_dir:
        Path(files_dir, 'theirs.mp3').write_bytes(b'x')
        Path(files_dir, 'theirs.srt').write_text(SRT_LYRICS, encoding='utf-8')
        Path(files_dir, 'clip.mp4').write_bytes(b'x')
        Path(files_dir, 'clip.srt').write_text(SRT_LYRICS, encoding='utf-8')
        stranger = other_user(db_path)
        user_store.record_media(db_path, 'theirs.mp3', stranger['id'])
        with patch('app.FILES_DIR', files_dir):
            others = client.get('/lyrics/theirs.mp3.lrc')
            video = client.get('/lyrics/clip.mp4.lrc')
            missing = client.get('/lyrics/nothing.mp3.lrc')

    assert others.status_code == 404
    assert video.status_code == 404
    assert missing.status_code == 404


def test_media_list_marks_items_that_can_be_summarized():
    with logged_in_client() as (client, _user, _db), TemporaryDirectory() as files_dir:
        for name in ['clip.mp4', 'plain.mp4', 'song.mp3', 'silent.mp3']:
            Path(files_dir, name).write_bytes(b'x')
        for name in ['clip.zh-Hans.srt', 'song.lrc']:
            Path(files_dir, name).write_text(
                '1\n00:00:01,000 --> 00:00:02,000\n你好\n', encoding='utf-8'
            )
        with (
            patch('app.FILES_DIR', files_dir),
            patch('app._probe_embedded_subtitles', return_value=()),
        ):
            payload = client.get('/api/media_list').get_json()

    items = {item['filename']: item for item in payload['video'] + payload['audio']}
    assert items['clip.mp4']['ai_summary'] is True
    assert items['plain.mp4']['ai_summary'] is False
    assert items['song.mp3']['ai_summary'] is True
    assert items['silent.mp3']['ai_summary'] is False


def test_library_has_ai_summary_panel_and_configured_flag():
    with logged_in_client() as (client, _user, _db):
        with patch('app.ai_summary_is_configured', return_value=True):
            enabled = client.get('/player').get_data(as_text=True)
        with patch('app.ai_summary_is_configured', return_value=False):
            disabled = client.get('/player').get_data(as_text=True)

    assert '<section class="dl-summary"' in enabled
    assert 'data-summary="generate"' in enabled
    assert bootstrap_payload(enabled)['aiSummary'] is True
    assert bootstrap_payload(disabled)['aiSummary'] is False
    script = Path(app_module.app.static_folder, 'dropload.js').read_text(
        encoding='utf-8',
    )
    assert "fetch('/api/ai_summary'," in script
    assert "'/api/ai_summary/jobs/' + encodeURIComponent(jobId) + '/stream'" in script
    assert 'renderSummaryPanel(item);' in script
