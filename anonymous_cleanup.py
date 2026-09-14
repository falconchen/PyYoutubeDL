"""匿名下载产物归属同步与定期清理。"""

import json
import os
import time

import user_store


def sync_media_ownership(config, logger):
    """依据任务结果清单补齐匿名媒体归属。"""
    user_store.init_db(config['USER_DB_PATH'])
    try:
        entries = os.listdir(config['URLS_DIR'])
    except OSError as exc:
        logger.error('扫描匿名任务结果失败: %s', exc)
        return

    for filename in entries:
        if not filename.endswith('.result.json'):
            continue
        task_id = filename[:-len('.result.json')]
        anonymous_id = user_store.anonymous_task_owner(
            config['USER_DB_PATH'], task_id
        )
        if not anonymous_id:
            continue
        result_path = os.path.join(config['URLS_DIR'], filename)
        try:
            with open(result_path, 'r', encoding='utf-8') as result_file:
                result = json.load(result_file)
        except (OSError, ValueError):
            continue
        for media_filename in result.get('files', []):
            if (
                isinstance(media_filename, str)
                and media_filename == os.path.basename(media_filename)
            ):
                user_store.record_anonymous_media(
                    config['USER_DB_PATH'],
                    media_filename,
                    anonymous_id,
                    task_id,
                    created_at=(
                        os.path.getmtime(
                            os.path.join(config['FILES_DIR'], media_filename)
                        )
                        if os.path.isfile(
                            os.path.join(config['FILES_DIR'], media_filename)
                        )
                        else None
                    ),
                )


def cleanup_expired_media(config, logger):
    """同步归属并删除超过匿名保留期的本地文件。"""
    hours = config.get('ANONYMOUS_FILES_EXPIRE_HOURS', 24)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
        hours = 24
    sync_media_ownership(config, logger)
    cutoff = int(time.time() - hours * 3600)
    for filename in user_store.expired_anonymous_media(
        config['USER_DB_PATH'], cutoff
    ):
        if filename != os.path.basename(filename):
            continue
        filepath = os.path.join(config['FILES_DIR'], filename)
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
                logger.info('已清理匿名过期文件: %s', filepath)
            user_store.delete_anonymous_media(
                config['USER_DB_PATH'], filename
            )
        except OSError as exc:
            logger.error('清理匿名过期文件失败 %s: %s', filepath, exc)


def cleanup_loop(config, logger, stop_event, interval_seconds=3600):
    """立即清理一次，之后按固定间隔重复，直到 stop_event 被设置。"""
    cleanup_expired_media(config, logger)
    while not stop_event.wait(interval_seconds):
        cleanup_expired_media(config, logger)
