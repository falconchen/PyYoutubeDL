#!venv/bin/python
"""播放列表监控 worker：轮询 YouTube Data API 的收件箱播放列表并消费式下发下载任务。

行为与 PHP 版 ProcessPlaylist.php 对齐：
- 读取 OAuth 令牌并在过期时自动刷新
- 遍历 MONITOR_PLAYLISTS 中的每个 playlistId -> [types]
- 对每条视频：先删除播放列表条目（消费式），删除成功再写入下载队列
- 刷新失败写 fail-lock 并 Bark 通知，等待用户重新授权后自动恢复
"""

import json
import signal
import time

from config_util import is_playlist_monitor_enabled, load_config
from log_util import setup_logger

import task_queue
import youtube_auth

from googleapiclient.errors import HttpError

ALLOWED_TYPES = {"video", "audio"}


class PlaylistMonitor:
    def __init__(self, config):
        self.config = config
        self.logger = setup_logger(
            name="playlist_monitor",
            log_dir=config["LOG_DIR"],
            log_file="playlist_monitor.log",
            max_bytes=config["MAX_LOG_SIZE"],
            backup_count=config["BACKUP_COUNT"],
            timezone=config.get("TIMEZONE", "UTC"),
        )
        self.url_dir = config["URLS_DIR"]
        self.timezone = config.get("TIMEZONE", "Asia/Shanghai")
        self._stop = False

    def notify(self, title, content):
        try:
            import bark_util

            device_token = self.config.get("BARK_DEVICE_TOKEN")
            if device_token:
                bark_util.bark_notify(device_token, title, content)
        except Exception as exc:
            self.logger.warning("Bark 通知发送失败: %s", exc)

    def stop(self):
        self._stop = True

    def run(self):
        while not self._stop:
            try:
                self._run_once()
            except Exception as exc:
                self.logger.exception("播放列表轮询异常: %s", exc)
            self._sleep(self.config.get("PLAYLIST_POLL_INTERVAL_SECONDS", 300))

    def _run_once(self):
        self.config = load_config()
        self.url_dir = self.config["URLS_DIR"]
        self.timezone = self.config.get("TIMEZONE", "Asia/Shanghai")

        if not is_playlist_monitor_enabled(self.config):
            self.logger.info("OAuth 或播放列表未配置，跳过本轮（等待配置）。")
            return

        if youtube_auth.fail_lock_exists(self.config):
            self.logger.warning(
                "存在刷新失败锁，请重新授权: %s",
                youtube_auth.get_oauth_start_url(self.config),
            )
            return

        creds = youtube_auth.get_credentials(self.config, notify=self.notify)
        if creds is None:
            self.logger.warning(
                "未找到 OAuth 令牌，请先访问 %s 授权。",
                youtube_auth.get_oauth_start_url(self.config),
            )
            return

        service = youtube_auth.build_youtube_service(self.config, creds)
        # 若尚未保存用户信息（例如本功能上线前已授权），补拉一次头像/名称
        if not youtube_auth.load_user_profile(self.config):
            profile = youtube_auth.fetch_user_profile(self.config, creds)
            if profile:
                youtube_auth.save_user_profile(self.config, profile)

        playlists = self.config.get("MONITOR_PLAYLISTS") or {}
        for playlist_id, types in playlists.items():
            if not playlist_id:
                continue
            try:
                self._process_playlist(
                    service,
                    playlist_id,
                    [t for t in (types or []) if t in ALLOWED_TYPES],
                )
            except HttpError as exc:
                self._handle_http_error(exc, playlist_id)

    def _process_playlist(self, service, playlist_id, types):
        max_items = self._valid_max_items(
            self.config.get("PLAYLIST_MAX_ITEMS_PER_RUN", 10)
        )
        response = (
            service.playlistItems()
            .list(part="snippet", playlistId=playlist_id, maxResults=max_items)
            .execute()
        )
        items = response.get("items") or []
        for item in items:
            try:
                self._consume_item(service, item, types)
            except HttpError as exc:
                self.logger.warning(
                    "处理播放列表 %s 条目 %s 失败: %s",
                    playlist_id,
                    item.get("id"),
                    exc,
                )
            except Exception as exc:
                self.logger.exception("处理条目异常: %s", exc)

    def _consume_item(self, service, item, types):
        snippet = item.get("snippet") or {}
        resource_id = snippet.get("resourceId") or {}
        if resource_id.get("kind") != "youtube#video":
            self.logger.warning("跳过非视频条目: %s", item.get("id"))
            return

        video_id = resource_id.get("videoId")
        if not video_id:
            return

        title = snippet.get("title") or video_id
        url = f"https://www.youtube.com/watch?v={video_id}"
        item_id = item.get("id")

        # 消费式：先删除，删除成功才下发下载任务（与 ProcessPlaylist.php 一致）
        if item_id:
            service.playlistItems().delete(id=item_id).execute()

        task_ids = task_queue.create_tasks(
            [url], types, self.url_dir, self.timezone
        )
        self.logger.info("已下发下载 %s (%s): %s", title, ",".join(types), url)
        self.notify(f"已加入下载: {title}", url)
        return task_ids

    def _handle_http_error(self, exc, playlist_id):
        reason = self._extract_error_reason(exc)
        self.logger.warning(
            "播放列表 %s 请求失败 (%s): %s", playlist_id, reason, exc
        )
        if reason == "quotaExceeded":
            self.logger.warning("YouTube API 配额耗尽，本轮延后，避免继续请求。")

    @staticmethod
    def _extract_error_reason(exc):
        try:
            content = exc.content
            if isinstance(content, bytes):
                content = content.decode("utf-8", "replace")
            payload = json.loads(content)
            errors = payload.get("error", {}).get("errors") or []
            return errors[0].get("reason", "") if errors else ""
        except Exception:
            return ""

    @staticmethod
    def _valid_max_items(value):
        if isinstance(value, bool):
            return 10
        try:
            value = int(value)
        except (TypeError, ValueError):
            return 10
        return value if value > 0 else 10

    def _sleep(self, seconds):
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            seconds = 300
        deadline = time.time() + max(0, seconds)
        while not self._stop and time.time() < deadline:
            time.sleep(1)


def main():
    config = load_config()
    monitor = PlaylistMonitor(config)

    def _handle_signal(signum, frame):
        monitor.logger.info("收到信号 %s，正在退出...", signum)
        monitor.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    monitor.run()


if __name__ == "__main__":
    main()
