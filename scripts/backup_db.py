#!/usr/bin/env python3
"""
Automated Database Backup Procedure (TASK-P15-02)

Performs zero-downtime, consistent SQLite snapshots using SQLite's online
backup API (sqlite3.Connection.backup).
Safely captures WAL buffers, verifies snapshot integrity, and enforces a
rolling retention policy (default: 7 days).

Usage:
    python scripts/backup_db.py [--db-path PATH] [--backup-dir DIR] [--retention-days DAYS]
"""
import os
import sys
import glob
import time
import sqlite3
import logging
import argparse
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backup_db")


def resolve_default_db_path() -> str:
    """Resolve SQLite database path from DATABASE_URL or default data directory."""
    db_url = os.getenv("DATABASE_URL", "")
    if db_url.startswith("sqlite:///"):
        return db_url.replace("sqlite:///", "")
    
    # Default to data/delta_terminal.sqlite in repository root
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(repo_root, "data", "delta_terminal.sqlite")


def resolve_default_backup_dir() -> str:
    """Resolve default backup directory."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(repo_root, "backups")


def create_sqlite_backup(
    source_db_path: str,
    backup_dir: str,
    retention_days: int = 7,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Perform online SQLite backup, verify integrity, and rotate old backups.

    Args:
        source_db_path: Absolute or relative path to SQLite database.
        backup_dir: Destination folder for backups.
        retention_days: Number of days to retain backups before deleting.
        dry_run: If True, simulates backup without writing or deleting.

    Returns:
        Summary dict containing backup status, destination path, and rotated files.
    """
    start_time = time.time()
    source_db_path = os.path.abspath(source_db_path)
    backup_dir = os.path.abspath(backup_dir)

    result: Dict[str, Any] = {
        "success": False,
        "source_db": source_db_path,
        "backup_path": None,
        "backup_size_bytes": 0,
        "integrity_ok": False,
        "rotated_backups": [],
        "duration_seconds": 0.0,
        "error": None,
    }

    if not os.path.exists(source_db_path):
        err = f"Source database file not found: {source_db_path}"
        logger.error(err)
        result["error"] = err
        return result

    os.makedirs(backup_dir, exist_ok=True)
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"delta_terminal_{timestamp_str}.db"
    dest_path = os.path.join(backup_dir, backup_filename)

    if dry_run:
        logger.info(f"[DRY-RUN] Would backup {source_db_path} to {dest_path}")
        result["success"] = True
        result["backup_path"] = dest_path
        return result

    logger.info(f"Starting online backup of {source_db_path} -> {dest_path}")

    source_conn: Optional[sqlite3.Connection] = None
    dest_conn: Optional[sqlite3.Connection] = None

    try:
        # Open source database in URI read-only mode to prevent locks
        source_conn = sqlite3.connect(f"file:{source_db_path}?mode=ro", uri=True)
        dest_conn = sqlite3.connect(dest_path)

        # Execute safe online backup (copies WAL buffers consistently)
        with dest_conn:
            source_conn.backup(dest_conn, pages=100)

        dest_conn.close()
        dest_conn = None
        source_conn.close()
        source_conn = None

        # Verify integrity of newly created snapshot
        verify_conn = sqlite3.connect(dest_path)
        cursor = verify_conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        check_row = cursor.fetchone()
        verify_conn.close()

        if check_row and check_row[0].lower() == "ok":
            result["integrity_ok"] = True
            logger.info(f"Integrity check passed for {backup_filename}")
        else:
            raise ValueError(f"Integrity check failed: {check_row}")

        file_size = os.path.getsize(dest_path)
        result["backup_path"] = dest_path
        result["backup_size_bytes"] = file_size
        logger.info(f"Backup created successfully: {dest_path} ({file_size} bytes)")

        # Rotate old backups
        rotated = rotate_backups(backup_dir, retention_days)
        result["rotated_backups"] = rotated
        result["success"] = True

    except Exception as e:
        logger.error(f"Backup failed: {e}", exc_info=True)
        result["error"] = str(e)
        if os.path.exists(dest_path) and not result["integrity_ok"]:
            try:
                os.remove(dest_path)
                logger.warning(f"Removed corrupt backup file: {dest_path}")
            except OSError:
                pass
    finally:
        if dest_conn:
            dest_conn.close()
        if source_conn:
            source_conn.close()
        result["duration_seconds"] = round(time.time() - start_time, 3)

    return result


def rotate_backups(backup_dir: str, retention_days: int) -> List[str]:
    """Delete backups older than retention_days."""
    rotated = []
    cutoff_time = time.time() - (retention_days * 86400)
    pattern = os.path.join(backup_dir, "delta_terminal_*.db")

    for filepath in glob.glob(pattern):
        try:
            mtime = os.path.getmtime(filepath)
            if mtime < cutoff_time:
                os.remove(filepath)
                rotated.append(filepath)
                logger.info(f"Pruned expired backup: {filepath} (older than {retention_days} days)")
        except OSError as e:
            logger.warning(f"Failed to remove expired backup {filepath}: {e}")

    return rotated


def main():
    parser = argparse.ArgumentParser(description="Automated SQLite Database Backup with Retention")
    parser.add_argument("--db-path", default=None, help="Path to source SQLite database")
    parser.add_argument("--backup-dir", default=None, help="Directory to store snapshots")
    parser.add_argument("--retention-days", type=int, default=7, help="Retention period in days (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate backup without copying")

    args = parser.parse_args()

    db_path = args.db_path or resolve_default_db_path()
    backup_dir = args.backup_dir or resolve_default_backup_dir()

    res = create_sqlite_backup(
        source_db_path=db_path,
        backup_dir=backup_dir,
        retention_days=args.retention_days,
        dry_run=args.dry_run,
    )

    if res["success"]:
        logger.info(f"Backup completed successfully in {res['duration_seconds']}s")
        sys.exit(0)
    else:
        logger.error(f"Backup failed: {res['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
