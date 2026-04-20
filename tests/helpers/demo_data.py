from __future__ import annotations

import csv
from pathlib import Path

from shinkoku.db import init_db
from shinkoku.master_accounts import MASTER_ACCOUNTS
from shinkoku.models import JournalEntry, JournalLine, OpeningBalanceInput
from shinkoku.tools.import_data import import_csv, import_record_source
from shinkoku.tools.ledger import (
    ledger_add_journal,
    ledger_set_opening_balances_batch,
    ledger_update_journal,
)


def _load_master_accounts(db_path: str, fiscal_year: int) -> None:
    conn = init_db(db_path)
    try:
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
        conn.execute("INSERT OR IGNORE INTO fiscal_years (year) VALUES (?)", (fiscal_year,))
        conn.commit()
    finally:
        conn.close()


def _map_expense_hint(account_hint: str | None, description: str) -> str:
    hint = (account_hint or "").strip()
    if hint == "外注費":
        return "5300"
    if hint == "仕入高":
        return "5001"
    desc = description.upper()
    if any(token in desc for token in ["OPENAI", "ANTHROPIC", "MIDJOURNEY", "ADOBE", "ENVATO"]):
        return "5140"
    if any(token in description for token in ["電車", "バス", "タクシー", "JR", "SUICA", "PASMO"]):
        return "5130"
    if any(token in description for token in ["素材", "備品", "文具", "ノート", "ストア"]):
        return "5190"
    return "5270"


def _map_deposit_credit_code(description: str, source_file: str) -> str:
    source_name = Path(source_file).name.lower()
    if source_name.endswith(".csv") and any(token in source_file for token in ["mufg_bank", "smbc_bank"]):
        if "振込入金" in description:
            return "1010"
        if "利息" in description:
            return "4100"
        if any(token in description for token in ["資金移動", "ｼｷﾝｲﾄﾞｳ", "口座移動"]):
            return "1001"
        return "4110"
    return "4001"


def _candidate_to_entry(candidate: dict, source_file: str) -> JournalEntry | None:
    amount = int(candidate.get("amount") or 0)
    if amount <= 0:
        return None

    date = str(candidate.get("date") or "")
    description = str(candidate.get("description") or "")
    counterparty = candidate.get("counterparty")
    direction = candidate.get("direction")

    debit_code: str
    credit_code: str
    if direction == "deposit":
        debit_code, credit_code = "1002", _map_deposit_credit_code(description, source_file)
    elif direction == "withdrawal":
        debit_code, credit_code = _map_expense_hint(candidate.get("account_hint"), description), "1002"
    elif direction == "sales":
        debit_code, credit_code = "1010", "4001"
    elif direction == "purchase":
        debit_code, credit_code = _map_expense_hint(candidate.get("account_hint"), description), "2030"
    elif direction == "receipt":
        debit_code, credit_code = "1002", "1010"
    elif direction == "payment":
        debit_code, credit_code = "2030", "1002"
    elif direction == "debit":
        debit_code, credit_code = _map_expense_hint(candidate.get("account_hint"), description), "1002"
    elif direction == "credit":
        debit_code, credit_code = "1002", "4001"
    else:
        debit_code, credit_code = _map_expense_hint(candidate.get("account_hint"), description), "2030"

    return JournalEntry(
        date=date,
        description=description,
        counterparty=str(counterparty) if counterparty else None,
        lines=[
            JournalLine(side="debit", account_code=debit_code, amount=amount),
            JournalLine(side="credit", account_code=credit_code, amount=amount),
        ],
        source="csv_import",
        source_file=source_file,
    )


def _load_csv_candidates(db_path: str, fiscal_year: int, sample_file: Path) -> None:
    res = import_csv(file_path=str(sample_file))
    if res.get("status") != "ok":
        return

    added_count = 0
    for candidate in res.get("candidates", []):
        entry = _candidate_to_entry(candidate, str(sample_file))
        if entry is None:
            continue
        result = ledger_add_journal(db_path=db_path, fiscal_year=fiscal_year, entry=entry, force=True)
        if result.get("status") == "ok":
            added_count += 1

    import_record_source(
        db_path=db_path,
        fiscal_year=fiscal_year,
        file_path=str(sample_file),
        row_count=added_count,
    )


def _load_fixed_assets_sample(db_path: str, fiscal_year: int, sample_file: Path) -> None:
    conn = init_db(db_path)
    try:
        with sample_file.open("r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                conn.execute(
                    "INSERT INTO fixed_assets "
                    "(name, acquisition_date, acquisition_cost, useful_life, method, "
                    "business_use_ratio, accumulated_depreciation, fiscal_year, memo) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["name"],
                        row["acquisition_date"],
                        int(row["acquisition_cost"]),
                        int(row["useful_life"]),
                        row["method"],
                        int(row["business_use_ratio"]),
                        int(row["accumulated_depreciation"]),
                        fiscal_year,
                        row.get("memo", ""),
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def _seed_opening_balances(db_path: str, fiscal_year: int) -> None:
    ledger_set_opening_balances_batch(
        db_path=db_path,
        fiscal_year=fiscal_year,
        balances=[
            OpeningBalanceInput(account_code="1002", amount=500000),
            OpeningBalanceInput(account_code="3001", amount=500000),
        ],
    )


def _seed_audit_and_duplicates(db_path: str, fiscal_year: int, samples_dir: Path) -> None:
    target_file = samples_dir / "erpnext" / "sales_invoices.csv"
    conn = init_db(db_path)
    try:
        row = conn.execute(
            "SELECT j.id, j.date, COALESCE(j.description,''), COALESCE(j.counterparty,''), "
            "j.source, COALESCE(j.source_file,''), jl.side, jl.account_code, jl.amount "
            "FROM journals j "
            "INNER JOIN journal_lines jl ON jl.journal_id = j.id "
            "WHERE j.fiscal_year = ? AND j.source_file = ? "
            "ORDER BY j.id, jl.id",
            (fiscal_year, str(target_file)),
        ).fetchall()
    finally:
        conn.close()

    if row:
        journal_id = int(row[0][0])
        lines = [
            JournalLine(side=str(item[6]), account_code=str(item[7]), amount=int(item[8]))
            for item in row
            if int(item[0]) == journal_id
        ]
        updated = JournalEntry(
            date=str(row[0][1]),
            description=str(row[0][2]),
            counterparty="株式会社青葉工務店",
            lines=lines,
            source=str(row[0][4] or "csv_import"),
            source_file=str(row[0][5]) or None,
        )
        ledger_update_journal(
            db_path=db_path,
            journal_id=journal_id,
            fiscal_year=fiscal_year,
            entry=updated,
        )

    dup_a = JournalEntry(
        date="2025-03-27",
        description="重複候補サンプルA",
        lines=[
            JournalLine(side="debit", account_code="5190", amount=50000),
            JournalLine(side="credit", account_code="1002", amount=50000),
        ],
        source="manual",
    )
    dup_b = JournalEntry(
        date="2025-03-27",
        description="重複候補サンプルB",
        lines=[
            JournalLine(side="debit", account_code="5270", amount=50000),
            JournalLine(side="credit", account_code="1001", amount=50000),
        ],
        source="manual",
    )
    ledger_add_journal(db_path=db_path, fiscal_year=fiscal_year, entry=dup_a, force=True)
    ledger_add_journal(db_path=db_path, fiscal_year=fiscal_year, entry=dup_b, force=True)


def build_capture_demo_db(
    *,
    db_path: str,
    fiscal_year: int,
    samples_dir: str,
    reference_export_path: str | None = None,
) -> dict[str, int | str]:
    sample_root = Path(samples_dir).resolve()
    db_file = Path(db_path).resolve()
    if db_file.exists():
        db_file.unlink()

    _load_master_accounts(str(db_file), fiscal_year)
    _seed_opening_balances(str(db_file), fiscal_year)

    sample_files = [
        sample_root / "erpnext" / "sales_invoices.csv",
        sample_root / "erpnext" / "purchase_invoices.csv",
        sample_root / "mufg_bank" / "9999999_sample.csv",
        sample_root / "smbc_bank" / "meisai.csv",
        sample_root / "aeon_card" / "meisai202502.csv",
        sample_root / "sumitomo_visa" / "202502.csv",
        sample_root / "sumitomo_visa" / "202503.csv",
        sample_root / "sumitomo_visa" / "202504.csv",
        sample_root / "rakuten_card" / "enavi202502_9999.csv",
        sample_root / "view_card" / "statement_202502.csv",
    ]
    for sample_file in sample_files:
        _load_csv_candidates(str(db_file), fiscal_year, sample_file)

    _load_fixed_assets_sample(str(db_file), fiscal_year, sample_root / "fixed_assets" / "fixed_assets.csv")
    _seed_audit_and_duplicates(str(db_file), fiscal_year, sample_root)

    conn = init_db(str(db_file))
    try:
        journal_count = int(
            conn.execute("SELECT COUNT(*) FROM journals WHERE fiscal_year = ?", (fiscal_year,)).fetchone()[0]
        )
        fixed_asset_count = int(
            conn.execute("SELECT COUNT(*) FROM fixed_assets WHERE fiscal_year = ?", (fiscal_year,)).fetchone()[0]
        )
        adjusted_count = int(conn.execute("SELECT COUNT(*) FROM journal_audit_log").fetchone()[0])
    finally:
        conn.close()

    return {
        "status": "ok",
        "db_path": str(db_file),
        "fiscal_year": fiscal_year,
        "journal_count": journal_count,
        "fixed_asset_count": fixed_asset_count,
        "adjusted_count": adjusted_count,
    }
