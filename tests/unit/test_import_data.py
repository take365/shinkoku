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
