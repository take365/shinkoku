from __future__ import annotations

from pathlib import Path

from shinkoku.web.app import (
    _filter_journals_by_query,
    _parse_journal_query,
    _set_query_sort,
    _source_file_aliases,
)


def test_source_file_aliases_adds_wsl_alias_for_windows_path(tmp_path: Path) -> None:
    base_dir = tmp_path

    aliases = _source_file_aliases(
        r"C:\Users\taker\Downloads\journals (6).csv",
        base_dir=base_dir,
    )

    assert r"C:\Users\taker\Downloads\journals (6).csv" in aliases
    assert "/mnt/c/Users/taker/Downloads/journals (6).csv" in aliases


def test_source_file_aliases_adds_windows_alias_for_wsl_path(tmp_path: Path) -> None:
    base_dir = tmp_path

    aliases = _source_file_aliases(
        "/mnt/d/project/shinkoku/shinkoku/input_samples/aeon_card/meisai202502.csv",
        base_dir=base_dir,
    )

    assert "/mnt/d/project/shinkoku/shinkoku/input_samples/aeon_card/meisai202502.csv" in aliases
    assert r"D:\project\shinkoku\shinkoku\input_samples\aeon_card\meisai202502.csv" in aliases


def test_parse_journal_query_parses_structured_terms() -> None:
    parsed = _parse_journal_query(
        'acc:1112 cat:asset desc:"ノート PC" cp:青葉 src:csv_import '
        'file:/mnt/c/tmp/a.csv from:2025-01-01 to:2025-01-31 mfrom:1000 mto:5000 '
        'sort:date desc freeword -除外'
    )

    assert parsed["account_code"] == "1112"
    assert parsed["category"] == "asset"
    assert parsed["description_terms"] == ["ノート PC"]
    assert parsed["counterparty_terms"] == ["青葉"]
    assert parsed["source"] == "csv_import"
    assert parsed["source_file"] == "/mnt/c/tmp/a.csv"
    assert parsed["date_from"] == "2025-01-01"
    assert parsed["date_to"] == "2025-01-31"
    assert parsed["amount_min"] == "1000"
    assert parsed["amount_max"] == "5000"
    assert parsed["sort"] == "date"
    assert parsed["dir"] == "desc"
    assert parsed["free_terms"] == ["freeword"]
    assert parsed["exclude_terms"] == ["除外"]
    assert parsed["q"] == "freeword"


def test_filter_journals_by_query_matches_and_excludes() -> None:
    account_names = {"1112": "普通預金", "5270": "消耗品費"}
    journals = [
        {
            "id": 1,
            "date": "2025-01-04",
            "description": "文具ステーション デザインノート",
            "counterparty": "株式会社青葉工務店",
            "source": "csv_import",
            "source_file": "/mnt/c/tmp/a.csv",
            "lines": [
                {"side": "debit", "account_code": "5270", "amount": 2480},
                {"side": "credit", "account_code": "1112", "amount": 2480},
            ],
        },
        {
            "id": 2,
            "date": "2025-01-05",
            "description": "除外したい旅費",
            "counterparty": "別取引先",
            "source": "manual",
            "source_file": "",
            "lines": [
                {"side": "debit", "account_code": "5270", "amount": 1200},
                {"side": "credit", "account_code": "1112", "amount": 1200},
            ],
        },
    ]

    filtered = _filter_journals_by_query(
        journals,
        free_terms=["デザインノート", "普通預金"],
        exclude_terms=["除外"],
        description_terms=["文具ステーション"],
        counterparty_terms=["青葉工務店"],
        account_names=account_names,
    )

    assert [journal["id"] for journal in filtered] == [1]


def test_set_query_sort_replaces_existing_sort_terms() -> None:
    result = _set_query_sort('desc:"ノート PC" sort:amount asc', "date", "desc")

    assert result == '"desc:ノート PC" sort:date desc'
