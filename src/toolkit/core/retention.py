"""保留策略（Sprint 4）：旧报告/旧备份/旧日志自动清理。

数据库设计文档规定：
- recovery_tasks + recovery_logs：保留 6 个月（等保审计）
- drill_runs：永久保留
- 本地报告文件：可配置保留天数（默认 90 天）
- 恢复机临时备份：可配置保留天数（默认 7 天）
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete

from toolkit.core.db import get_session_ctx
from toolkit.core.executor import CommandExecutor
from toolkit.core.logger import get_logger
from toolkit.core.models import Backup, RecoveryLog, RecoveryTask

logger = get_logger(__name__)


def _cutoff(days: int) -> str:
    """N 天前的 ISO8601 时间戳。"""
    from datetime import timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def cleanup_old_tasks(keep_days: int = 180) -> int:
    """清理超期的任务与日志记录（默认 6 个月，等保要求）。

    Returns:
        删除的任务数
    """
    cutoff = _cutoff(keep_days)
    session = get_session_ctx()
    try:
        # 先删日志（外键）
        old_logs = (
            session.query(RecoveryLog)
            .filter(RecoveryLog.created_at < cutoff)
            .all()
        )
        log_ids = [l.id for l in old_logs]
        if log_ids:
            session.query(RecoveryLog).filter(RecoveryLog.id.in_(log_ids)).delete(
                synchronize_session=False
            )

        # 再删已终态的旧任务（SUCCESS/FAILED_FINAL）
        result = (
            session.query(RecoveryTask)
            .filter(
                RecoveryTask.created_at < cutoff,
                RecoveryTask.status.in_(["SUCCESS", "FAILED_FINAL", "ARCHIVED"]),
            )
            .delete(synchronize_session=False)
        )
        session.commit()
        logger.info("清理 %d 条日志 + %d 条旧任务（>=%d 天）", len(log_ids), result, keep_days)
        return result
    except Exception as e:
        session.rollback()
        logger.error("清理旧任务失败: %s", e)
        return 0
    finally:
        session.close()


def cleanup_local_reports(reports_dir: str, keep_days: int = 90) -> int:
    """清理本地旧报告文件。

    Args:
        reports_dir: 报告目录（管理机本地）
        keep_days: 保留天数

    Returns:
        删除的文件数
    """
    from pathlib import Path

    reports_path = Path(reports_dir)
    if not reports_path.exists():
        return 0

    cutoff_ts = datetime.now() - timedelta(days=keep_days)
    removed = 0
    for f in reports_path.glob("drill-run*.md"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff_ts:
                f.unlink()
                removed += 1
        except OSError as e:
            logger.warning("删除报告失败 %s: %s", f, e)
    if removed:
        logger.info("清理 %d 个旧报告文件（>=%d 天）", removed, keep_days)
    return removed


def cleanup_remote_backups(
    executor: CommandExecutor,
    backup_root: str,
    keep_days: int = 7,
) -> int:
    """清理恢复机上超期的临时/演练备份目录。

    Args:
        executor: 连到恢复机的 SSHExecutor
        backup_root: 备份根目录（如 /data/drill/trigger-test）
        keep_days: 保留天数（临时备份默认 7 天）

    Returns:
        删除的目录数
    """
    # find 超期目录（mtime 早于 N 天前的）
    cmd = f"find {backup_root} -maxdepth 2 -mindepth 2 -type d -mtime +{keep_days} 2>/dev/null"
    res = executor.run(cmd)
    if not res.ok or not res.stdout.strip():
        return 0

    dirs = [d for d in res.stdout.strip().splitlines() if d.strip()]
    removed = 0
    for d in dirs:
        del_res = executor.run(f"rm -rf {d}")
        if del_res.ok:
            removed += 1
            logger.info("已清理超期备份目录: %s", d)
        else:
            logger.warning("清理失败: %s: %s", d, del_res.stderr)
    return removed
