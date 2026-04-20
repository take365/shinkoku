from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.helpers.demo_data import build_capture_demo_db


def test_build_capture_demo_db_creates_demo_content(tmp_path: Path) -> None:
    db_path = tmp_path / "capture_demo.db"
    samples_dir = Path("input_samples").resolve()

    result = build_capture_demo_db(
        db_path=str(db_path),
        fiscal_year=2025,
        samples_dir=str(samples_dir),
        reference_export_path=str(Path("output/exports/journals_2025.csv").resolve()),
    )

    assert result["status"] == "ok"
    assert result["journal_count"] > 10
    assert result["fixed_asset_count"] >= 1
    assert result["adjusted_count"] >= 1

    conn = sqlite3.connect(db_path)
    try:
        fixed_asset = conn.execute(
            "SELECT name, acquisition_cost FROM fixed_assets WHERE fiscal_year = 2025 ORDER BY id LIMIT 1"
        ).fetchone()
        assert fixed_asset == ("開発用ノートPC", 198000)

        total_sales = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM journal_lines "
            "WHERE side = 'credit' AND account_code = '4001'"
        ).fetchone()[0]
        assert total_sales == 1518000

        audit_count = conn.execute("SELECT COUNT(*) FROM journal_audit_log").fetchone()[0]
        assert audit_count >= 1

        imported_sources = conn.execute("SELECT COUNT(*) FROM import_sources").fetchone()[0]
        assert imported_sources >= 5
    finally:
        conn.close()
