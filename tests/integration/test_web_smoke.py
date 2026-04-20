from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from shinkoku.db import get_connection, init_db
from shinkoku.master_accounts import MASTER_ACCOUNTS
from shinkoku.models import JournalEntry, JournalLine, OpeningBalanceInput
from shinkoku.tools.ledger import (
    ledger_add_journal,
    ledger_set_opening_balances_batch,
    ledger_update_journal,
)
from shinkoku.web.app import create_app


def _load_accounts(conn: sqlite3.Connection) -> None:
    for account in MASTER_ACCOUNTS:
        conn.execute(
            "INSERT OR IGNORE INTO accounts (code, name, category, sub_category, tax_category) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                account["code"],
                account["name"],
                account["category"],
                account["sub_category"],
                account["tax_category"],
            ),
        )


def _seed_web_demo_db(db_path: str, source_file: str) -> int:
    conn = init_db(db_path)
    _load_accounts(conn)
    conn.execute("INSERT OR IGNORE INTO fiscal_years (year) VALUES (2025)")
    conn.execute(
        "INSERT INTO fixed_assets "
        "(name, acquisition_date, acquisition_cost, useful_life, method, "
        "business_use_ratio, accumulated_depreciation, fiscal_year, memo) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "開発用ノートPC",
            "2025-02-01",
            198000,
            4,
            "straight_line",
            100,
            0,
            2025,
            "画面確認用サンプル",
        ),
    )
    conn.execute(
        "INSERT INTO import_sources (fiscal_year, file_hash, file_name, file_path, row_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (2025, "demo-file-hash", Path(source_file).name, source_file, 1),
    )
    conn.commit()
    conn.close()

    ledger_set_opening_balances_batch(
        db_path=db_path,
        fiscal_year=2025,
        balances=[
            OpeningBalanceInput(account_code="1002", amount=500000),
            OpeningBalanceInput(account_code="3001", amount=500000),
        ],
    )

    sale = JournalEntry(
        date="2025-01-15",
        description="ウェブ開発報酬",
        lines=[
            JournalLine(side="debit", account_code="1002", amount=100000),
            JournalLine(side="credit", account_code="4001", amount=100000),
        ],
        source="csv_import",
        source_file=source_file,
    )
    sale_added = ledger_add_journal(db_path=db_path, fiscal_year=2025, entry=sale)
    sale_id = int(sale_added["journal_id"])

    sale_updated = JournalEntry(
        date="2025-01-15",
        description="ウェブ開発報酬",
        counterparty="株式会社青葉工務店",
        lines=[
            JournalLine(side="debit", account_code="1002", amount=100000),
            JournalLine(side="credit", account_code="4001", amount=100000),
        ],
        source="csv_import",
        source_file=source_file,
    )
    ledger_update_journal(
        db_path=db_path,
        journal_id=sale_id,
        fiscal_year=2025,
        entry=sale_updated,
    )

    internet = JournalEntry(
        date="2025-01-20",
        description="インターネット回線",
        counterparty="NTT東日本",
        lines=[
            JournalLine(side="debit", account_code="5140", amount=5000),
            JournalLine(side="credit", account_code="1002", amount=5000),
        ],
        source="manual",
    )
    ledger_add_journal(db_path=db_path, fiscal_year=2025, entry=internet)

    duplicate_a = JournalEntry(
        date="2025-02-10",
        description="文房具購入A",
        lines=[
            JournalLine(side="debit", account_code="5190", amount=3000),
            JournalLine(side="credit", account_code="1002", amount=3000),
        ],
        source="manual",
    )
    duplicate_b = JournalEntry(
        date="2025-02-10",
        description="文房具購入B",
        lines=[
            JournalLine(side="debit", account_code="5270", amount=3000),
            JournalLine(side="credit", account_code="1001", amount=3000),
        ],
        source="manual",
    )
    ledger_add_journal(db_path=db_path, fiscal_year=2025, entry=duplicate_a)
    ledger_add_journal(db_path=db_path, fiscal_year=2025, entry=duplicate_b, force=True)

    return sale_id


def test_web_smoke_basic_tour(tmp_path: Path) -> None:
    db_path = str(tmp_path / "web-smoke.db")
    source_file = tmp_path / "import_sample.csv"
    source_file.write_text("日付,摘要,金額\n2025-01-15,ウェブ開発報酬,100000\n", encoding="utf-8")
    sale_id = _seed_web_demo_db(db_path, str(source_file))

    conn = get_connection(db_path)
    try:
        audit_log_id = int(
            conn.execute(
                "SELECT id FROM journal_audit_log WHERE journal_id = ? ORDER BY id DESC LIMIT 1",
                (sale_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()

    client = TestClient(create_app(db_path=db_path, fiscal_year=2025))

    response = client.get("/journals")
    assert response.status_code == 200
    assert "ウェブ開発報酬" in response.text
    assert "株式会社青葉工務店" in response.text

    response = client.get("/journals", params={"query": "acc:1002"})
    assert response.status_code == 200
    assert "普通預金" in response.text

    response = client.get("/journals", params={"query": 'desc:"ウェブ開発報酬"'})
    assert response.status_code == 200
    assert "ウェブ開発報酬" in response.text

    response = client.get("/journals", params={"query": 'cp:"株式会社青葉工務店"'})
    assert response.status_code == 200
    assert "株式会社青葉工務店" in response.text

    response = client.get(
        "/journals",
        params={"query": "from:2025-01-01 to:2025-02-28 cat:expense"},
    )
    assert response.status_code == 200
    assert "インターネット回線" in response.text

    response = client.get("/journals.csv")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "journal_id,date,description" in response.text

    response = client.get("/trial-balance")
    assert response.status_code == 200
    assert "勘定科目サマリ" in response.text

    response = client.get("/gl/1002")
    assert response.status_code == 200
    assert "総勘定元帳" in response.text
    assert "普通預金" in response.text

    response = client.get("/summary/description")
    assert response.status_code == 200
    assert "摘要サマリ" in response.text

    response = client.get("/summary/counterparty")
    assert response.status_code == 200
    assert "取引先サマリ" in response.text

    response = client.get(
        "/summary/monthly",
        params={"date_from": "2025-01-01", "date_to": "2025-12-31", "category": "expense"},
    )
    assert response.status_code == 200
    assert "月次サマリ" in response.text

    response = client.get("/source-links")
    assert response.status_code == 200
    assert "入力元サマリ" in response.text
    assert source_file.name in response.text

    response = client.get("/source-links/detail", params={"path": str(source_file)})
    assert response.status_code == 200
    assert "入力元詳細" in response.text
    assert "ウェブ開発報酬" in response.text

    response = client.get("/pl")
    assert response.status_code == 200
    assert "損益計算書" in response.text

    response = client.get("/bs")
    assert response.status_code == 200
    assert "貸借対照表" in response.text
    assert "当期純利益" in response.text

    response = client.get("/duplicates", params={"threshold": 70})
    assert response.status_code == 200
    assert "重複候補" in response.text
    assert "文房具購入A" in response.text

    response = client.get("/fixed-assets")
    assert response.status_code == 200
    assert "固定資産台帳" in response.text
    assert "開発用ノートPC" in response.text

    response = client.get("/audit")
    assert response.status_code == 200
    assert "監査ログ" in response.text
    assert "update" in response.text

    response = client.get("/audit/detail", params={"log_id": audit_log_id})
    assert response.status_code == 200
    assert "株式会社青葉工務店" in response.text

    response = client.get("/audit/diff", params={"log_id": audit_log_id})
    assert response.status_code == 200
    assert "差分" in response.text or "before" in response.text or "after" in response.text
