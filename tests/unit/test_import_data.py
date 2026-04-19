from __future__ import annotations

from pathlib import Path

from shinkoku.tools.import_data import import_csv


def test_import_csv_aeon_card_section(tmp_path: Path) -> None:
    csv_text = """ご利用カード,イオンルネサンスＶＩＳＡカード,,,,,,,
今回ご請求金額,13530,,,,,,,
お支払い日,2025年 1月 6日,,,,,,,
ご利用明細,,,,,,,,
ご利用日,利用者区分,ご利用先,支払方法,,,ご利用金額,備考,
241130,本人,スポーツクラブルネサンス,１回,,,13530,２０２４／１２カイヒ,
分割・ボーナス払い明細,,,,,,,,
ご利用日,ご利用先,支払回数,ご利用金額,実質年率,お支払い総額,今回ご請求金額,内手数料,今回回数
"""
    path = tmp_path / "aeon.csv"
    path.write_text(csv_text, encoding="shift_jis")

    result = import_csv(file_path=str(path))

    assert result["status"] == "ok"
    assert result["errors"] == []
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["date"] == "2024-11-30"
    assert result["candidates"][0]["description"] == "スポーツクラブルネサンス ２０２４／１２カイヒ"
    assert result["candidates"][0]["amount"] == 13530


def test_import_csv_utf8_bom_header(tmp_path: Path) -> None:
    csv_text = (
        '"利用日","利用店名・商品名","利用者","支払方法","利用金額"\n'
        '"2024/12/24","STEAMGAMES.COM 42595利用国USA","本人","1回払い","880"\n'
    )
    path = tmp_path / "rakuten.csv"
    path.write_text(csv_text, encoding="utf-8-sig")

    result = import_csv(file_path=str(path))

    assert result["status"] == "ok"
    assert result["errors"] == []
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["date"] == "2024-12-24"
    assert result["candidates"][0]["description"] == "STEAMGAMES.COM 42595利用国USA"
    assert result["candidates"][0]["amount"] == 880


def test_import_csv_mufg_bank_includes_withdrawal_and_deposit(tmp_path: Path) -> None:
    csv_text = """日付,摘要,摘要内容,支払い金額,預かり金額,差引残高
2025/1/6,口座振替３,イオン,13530,,583716
2025/1/14,投信換金,ファンド,,3000000,3535079
"""
    path = tmp_path / "mufg.csv"
    path.write_text(csv_text, encoding="shift_jis")

    result = import_csv(file_path=str(path))

    assert result["status"] == "ok"
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["direction"] == "withdrawal"
    assert result["candidates"][0]["amount"] == 13530
    assert result["candidates"][1]["direction"] == "deposit"
    assert result["candidates"][1]["amount"] == 3000000


def test_import_csv_smbc_bank_includes_withdrawal_and_deposit(tmp_path: Path) -> None:
    csv_text = """年月日,お引出し,お預入れ,お取り扱い内容,残高
2025/12/26,4003,,ﾐﾂｲｽﾐﾄﾓｶ-ﾄﾞ (ｶ,82109
2025/12/7,,4,スーパー定期利息,86112
"""
    path = tmp_path / "smbc.csv"
    path.write_text(csv_text, encoding="shift_jis")

    result = import_csv(file_path=str(path))

    assert result["status"] == "ok"
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["direction"] == "withdrawal"
    assert result["candidates"][0]["amount"] == 4003
    assert result["candidates"][1]["direction"] == "deposit"
    assert result["candidates"][1]["amount"] == 4


def test_import_csv_erpnext_sales_invoices(tmp_path: Path) -> None:
    csv_text = """invoice_no,posting_date,customer_name,customer_registration_no,currency,net_amount,tax_amount,grand_total,tax_rate,tax_category,is_return
ACC-SINV-2026-00001,2026-03-15,王立騎士団,T1234567890123,JPY,100000,10000,110000,10.0,standard,0
ACC-SINV-2026-00002,2026-03-18,白銀聖堂,T2345678901234,JPY,5000,400,5400,8.0,reduced,1
"""
    path = tmp_path / "sales_invoices.csv"
    path.write_text(csv_text, encoding="utf-8")

    result = import_csv(file_path=str(path))

    assert result["status"] == "ok"
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["direction"] == "sales"
    assert result["candidates"][0]["amount"] == 110000
    assert result["candidates"][1]["direction"] == "refund"
    assert result["candidates"][1]["description"].startswith("返金")


def test_import_csv_erpnext_purchase_invoices(tmp_path: Path) -> None:
    csv_text = """bill_no,posting_date,supplier_name,supplier_registration_no,currency,net_amount,tax_amount,grand_total,tax_rate,tax_category,expense_account
ACC-PINV-2026-00012,2026-03-10,深緑薬草園,T0987654321098,JPY,40000,4000,44000,10.0,standard,仕入高
"""
    path = tmp_path / "purchase_invoices.csv"
    path.write_text(csv_text, encoding="utf-8")

    result = import_csv(file_path=str(path))

    assert result["status"] == "ok"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["direction"] == "purchase"
    assert result["candidates"][0]["amount"] == 44000
    assert result["candidates"][0]["account_hint"] == "仕入高"


def test_import_csv_erpnext_journal_entries(tmp_path: Path) -> None:
    csv_text = """journal_no,posting_date,account,party_type,party,debit,credit,remarks
ACC-JV-2026-00003,2026-12-31,減価償却費,,,120000,0,固定資産の期末減価償却
ACC-JV-2026-00003,2026-12-31,減価償却累計額,,,0,120000,固定資産の期末減価償却
"""
    path = tmp_path / "journal_entries.csv"
    path.write_text(csv_text, encoding="utf-8")

    result = import_csv(file_path=str(path))

    assert result["status"] == "ok"
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["direction"] == "debit"
    assert result["candidates"][1]["direction"] == "credit"
    assert result["candidates"][0]["amount"] == 120000


def test_import_csv_erpnext_payments(tmp_path: Path) -> None:
    csv_text = """payment_no,posting_date,party_type,party,mode_of_payment,paid_amount,received_amount,reference_doctype,reference_name
ACC-PAY-2026-00001,2026-03-20,Customer,王立騎士団,Bank,0,110000,Sales Invoice,ACC-SINV-2026-00001
ACC-PAY-2026-00002,2026-03-14,Supplier,深緑薬草園,Bank,44000,0,Purchase Invoice,ACC-PINV-2026-00012
"""
    path = tmp_path / "payments.csv"
    path.write_text(csv_text, encoding="utf-8")

    result = import_csv(file_path=str(path))

    assert result["status"] == "ok"
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["direction"] == "receipt"
    assert result["candidates"][0]["amount"] == 110000
    assert result["candidates"][1]["direction"] == "payment"
    assert result["candidates"][1]["amount"] == 44000
