"""
Phase 15 Tests: Deployment and Operational Readiness

Tests:
1. Automated SQLite database backup procedure (Task 15.2: Online backup, integrity check, retention)
2. Process supervision and deployment artifacts (Task 15.1: supervisord.conf, service.ps1, systemd unit)
"""
import os
import sys
import time
import sqlite3
import pytest
from datetime import datetime, timedelta

from scripts.backup_db import create_sqlite_backup, rotate_backups, resolve_default_backup_dir


class TestDatabaseBackupProcedure:
    """Verifies SQLite automated online backup, integrity verification, and rotation."""

    @pytest.fixture
    def temp_sqlite_db(self, tmp_path):
        """Create a temporary populated SQLite database."""
        db_file = tmp_path / "test_delta.sqlite"
        conn = sqlite3.connect(str(db_file))
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("CREATE TABLE test_table (id INTEGER PRIMARY KEY, name TEXT, val REAL);")
        cursor.execute("INSERT INTO test_table (name, val) VALUES ('btc_pos', 65000.5), ('eth_pos', 3400.2);")
        conn.commit()
        conn.close()
        return str(db_file)

    def test_sqlite_backup_creation_and_integrity(self, temp_sqlite_db, tmp_path):
        backup_dir = tmp_path / "backups"
        res = create_sqlite_backup(
            source_db_path=temp_sqlite_db,
            backup_dir=str(backup_dir),
            retention_days=7,
        )

        assert res["success"] is True
        assert res["error"] is None
        assert res["integrity_ok"] is True
        assert res["backup_path"] is not None
        assert os.path.exists(res["backup_path"])
        assert res["backup_size_bytes"] > 0

        # Verify data inside the backup
        b_conn = sqlite3.connect(res["backup_path"])
        b_cursor = b_conn.cursor()
        b_cursor.execute("SELECT COUNT(*) FROM test_table;")
        count = b_cursor.fetchone()[0]
        b_conn.close()
        assert count == 2

    def test_sqlite_backup_missing_source_fails_gracefully(self, tmp_path):
        fake_db = tmp_path / "non_existent.sqlite"
        backup_dir = tmp_path / "backups"

        res = create_sqlite_backup(
            source_db_path=str(fake_db),
            backup_dir=str(backup_dir),
            retention_days=7,
        )

        assert res["success"] is False
        assert "not found" in res["error"]

    def test_sqlite_backup_dry_run(self, temp_sqlite_db, tmp_path):
        backup_dir = tmp_path / "backups"

        res = create_sqlite_backup(
            source_db_path=temp_sqlite_db,
            backup_dir=str(backup_dir),
            retention_days=7,
            dry_run=True,
        )

        assert res["success"] is True
        assert res["backup_path"] is not None
        # In dry run, file is NOT created on disk
        assert not os.path.exists(res["backup_path"])

    def test_sqlite_backup_retention_rotation(self, tmp_path):
        backup_dir = tmp_path / "backups_rotate"
        os.makedirs(str(backup_dir), exist_ok=True)

        now = time.time()
        # Create an old backup (10 days old)
        old_file = backup_dir / "delta_terminal_20260101_120000.db"
        old_file.write_text("old_backup")
        os.utime(str(old_file), (now - 10 * 86400, now - 10 * 86400))

        # Create a fresh backup (1 day old)
        new_file = backup_dir / "delta_terminal_20260110_120000.db"
        new_file.write_text("fresh_backup")
        os.utime(str(new_file), (now - 1 * 86400, now - 1 * 86400))

        # Rotate with 7 days retention
        pruned = rotate_backups(str(backup_dir), retention_days=7)

        assert len(pruned) == 1
        assert str(old_file) in pruned[0]
        assert not os.path.exists(str(old_file))
        assert os.path.exists(str(new_file))


class TestProcessSupervisionArtifacts:
    """Verifies existence and structural integrity of production process supervisor configurations."""

    def test_supervisord_configuration(self):
        conf_path = os.path.join(os.path.dirname(__file__), "..", "deploy", "supervisord.conf")
        assert os.path.exists(conf_path)
        with open(conf_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "[program:delta-terminal]" in content
        assert "command=python app.py" in content
        assert "autorestart=unexpected" in content
        assert "stopsignal=TERM" in content

    def test_service_powershell_script(self):
        ps1_path = os.path.join(os.path.dirname(__file__), "..", "deploy", "service.ps1")
        assert os.path.exists(ps1_path)
        with open(ps1_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "System.Diagnostics.Process" in content
        assert "/api/v2/system/mode" in content
        assert "MaxRestarts" in content

    def test_systemd_service_unit(self):
        svc_path = os.path.join(os.path.dirname(__file__), "..", "deploy", "delta-terminal.service")
        assert os.path.exists(svc_path)
        with open(svc_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "[Unit]" in content
        assert "[Service]" in content
        assert "ExecStart=/opt/delta-terminal/venv/bin/python app.py" in content
        assert "Restart=always" in content

    def test_dockerfile_configuration(self):
        df_path = os.path.join(os.path.dirname(__file__), "..", "deploy", "Dockerfile")
        assert os.path.exists(df_path)
        with open(df_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "FROM python" in content
        assert "HEALTHCHECK" in content
        assert "EXPOSE 5000" in content
