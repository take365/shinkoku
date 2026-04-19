"""Data import tools for the shinkoku MCP server."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from shinkoku.db import get_connection
from shinkoku.duplicate_detection import check_source_file_imported, record_import_source
from shinkoku.hashing import compute_file_hash


def _detect_encoding(file_path: str) -> str:
    """Detect file encoding (UTF-8 or Shift_JIS)."""
    raw = Path(file_path).read_bytes()
    # Try UTF-8 first
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    # Try Shift_JIS
    try:
        raw.decode("shift_jis")
        return "shift_jis"
    except UnicodeDecodeError:
        pass
    # Fallback
    return "utf-8"


def _read_csv_text(file_path: str, encoding: str) -> str:
    """Read CSV text, handling UTF-8 BOM when present."""
    path = Path(file_path)
    if encoding == "utf-8":
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            return raw.decode("utf-8-sig")
    return path.read_text(encoding=encoding)


def _detect_date_column(headers: list[str]) -> int | None:
    """Find the column index that looks like a date column."""
    # Include common JP headings seen in card/bank CSVs
    date_patterns = [
        "日付",
        "利用日",
        "ご利用日",
        "ご利用年月日",
        "date",
        "取引日",
        "発生日",
        "年月日",
    ]
    for i, h in enumerate(headers):
        h_lower = h.lower().strip()
        for pattern in date_patterns:
            if pattern.lower() in h_lower:
                return i
    return None


def _detect_description_column(headers: list[str]) -> int | None:
    """Find the primary description-like column index.

    Prefer more specific columns (e.g., 摘要内容/お振込内容/取引内容) over a generic 摘要.
    """
    # Priority-ordered list (more specific first)
    desc_patterns = [
        "摘要内容",
        "お振込内容",
        "取引内容",
        "ご利用箇所",
        "支払先名称",
        "お支払先",
        "支払先",
        "利用店名",
        "店名",
        "振込依頼人",
        "名称",
        "備考",
        "description",
        "内容",
        "摘要",
    ]
    # Search by priority
    lowered = [h.lower().strip() for h in headers]
    for pattern in desc_patterns:
        p = pattern.lower()
        for idx, h in enumerate(lowered):
            if p in h:
                return idx
    return None


def _find_additional_desc_columns(headers: list[str], primary_idx: int | None) -> list[int]:
    """Find additional description-related columns to concatenate for better context.

    e.g., if both 摘要 and 摘要内容 exist, include both.
    """
    extra_patterns = [
        # include both summary and detail style columns; order matters
        "摘要",  # generic summary (prefer to show first later)
        "摘要内容",
        "お振込内容",
        "取引内容",
        "利用店名",
        "支払先名称",
        "お支払先",
        "支払先",
        "振込依頼人",
        "名称",
        "備考",
    ]
    lowered = [h.lower().strip() for h in headers]
    idxs: list[int] = []
    for pat in extra_patterns:
        p = pat.lower()
        for i, h in enumerate(lowered):
            if i == primary_idx:
                continue
            if p in h and i not in idxs:
                idxs.append(i)
    return idxs


def _detect_amount_column(headers: list[str]) -> int | None:
    """Find the column index for the amount."""
    amount_patterns = [
        "金額",
        "利用金額",
        "ご利用額",
        "ご請求額",
        "amount",
        "支払金額",
        "取引金額",
        "出金金額",
        "入金金額",
        "出金",
        "入金",
        "合計",
    ]
    for i, h in enumerate(headers):
        h_lower = h.lower().strip()
        for pattern in amount_patterns:
            if pattern.lower() in h_lower:
                return i
    return None


def _find_header_row(rows: list[list[str]]) -> tuple[list[str], int]:
    """Try to detect the header row index within the first few rows.

    Some JP card CSVs prepend metadata lines before the actual header. This
    scans up to 20 rows to find a plausible header by matching known keywords.
    Returns (headers, data_start_index).
    """
    max_scan = min(20, len(rows))
    for idx in range(max_scan):
        headers = [h.strip() for h in rows[idx]]
        if not headers or len(headers) < 2:
            continue
        d = _detect_date_column(headers)
        a = _detect_amount_column(headers)
        s = _detect_description_column(headers)
        # Require at least two signals (e.g., date+amount or amount+desc)
        signals = sum(x is not None for x in (d, a, s))
        if signals >= 2:
            return headers, idx + 1
    # Fallback to first row
    return ([h.strip() for h in rows[0]] if rows else []), 1


def _parse_amount(value: str) -> int | None:
    """Parse an amount string to int. Returns None if unparseable."""
    cleaned = value.strip().replace(",", "").replace("\\", "").replace("¥", "")
    cleaned = re.sub(r"[^\d\-]", "", cleaned)
    if not cleaned or cleaned == "-":
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _normalize_date(value: str) -> str | None:
    """Normalize date to YYYY-MM-DD format."""
    value = value.strip()
    # Already YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return value
    # YYYY/MM/DD
    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", value)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # YYYY年 M月 D日
    m = re.match(r"^(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日$", value)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # YYMMDD (credit card CSVs such as AEON)
    m = re.match(r"^(\d{2})(\d{2})(\d{2})$", value)
    if m:
        return f"20{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _build_original_data(headers: list[str], row: list[str]) -> dict[str, str]:
    original = {}
    for j, h in enumerate(headers):
        if j < len(row):
            original[h] = row[j].strip()
    return original


def _build_generic_description(
    headers: list[str],
    row: list[str],
    desc_col: int | None,
    extra_desc_cols: list[int],
) -> str:
    parts: list[str] = []
    try:
        summary_idx = next(i for i, h in enumerate(headers) if h.strip() == "摘要")
    except StopIteration:
        summary_idx = None  # type: ignore[assignment]

    if summary_idx is not None and summary_idx < len(row):
        v = row[summary_idx].strip()
        if v:
            parts.append(v)

    if desc_col is not None and desc_col < len(row) and desc_col != summary_idx:
        v = row[desc_col].strip()
        if v and v not in parts:
            parts.append(v)

    for j in extra_desc_cols:
        if j < len(row):
            v = row[j].strip()
            if v and v not in parts:
                parts.append(v)

    return " ".join(parts)


def _result_dict(
    *,
    file_path: str,
    file_hash: str,
    encoding: str,
    candidates: list[dict],
    skipped_rows: list[int],
    errors: list[str],
) -> dict:
    return {
        "status": "ok",
        "file_path": file_path,
        "file_hash": file_hash,
        "encoding": encoding,
        "total_rows": len(candidates),
        "candidates": candidates,
        "skipped_rows": skipped_rows,
        "errors": errors,
    }


def _detect_import_format(rows: list[list[str]]) -> str:
    """Detect vendor-specific CSV formats before generic parsing."""
    for row in rows[:10]:
        cells = [cell.strip() for cell in row]
        if not cells:
            continue
        first = cells[0] if cells else ""
        if first == "ご利用カード":
            return "aeon_card"
    return "generic"


def _import_generic_csv(*, file_path: str, encoding: str, rows: list[list[str]]) -> dict:
    """Generic CSV parser used as the default fallback."""
    # Detect header row (handles files with leading metadata lines)
    headers, data_start = _find_header_row(rows)
    date_col = _detect_date_column(headers)
    desc_col = _detect_description_column(headers)
    # Find extra description-like columns to concatenate
    extra_desc_cols = _find_additional_desc_columns(headers, desc_col)
    amount_col = _detect_amount_column(headers)
    # Fallback defaults if still not detected
    if date_col is None and headers:
        date_col = 0
    if desc_col is None and len(headers) > 1:
        desc_col = 1
    if amount_col is None and len(headers) > 2:
        amount_col = 2

    candidates = []
    skipped_rows = []
    errors: list[str] = []

    for i, row in enumerate(rows[data_start:], start=data_start + 1):
        # Skip empty rows
        if not row or all(not cell.strip() for cell in row):
            continue

        try:
            # Validate we have enough columns
            if (
                date_col is not None
                and date_col >= len(row)
                or desc_col is not None
                and desc_col >= len(row)
                or amount_col is not None
                and amount_col >= len(row)
            ):
                skipped_rows.append(i)
                continue

            date_val = _normalize_date(row[date_col]) if date_col is not None else None
            desc_val = _build_generic_description(headers, row, desc_col, extra_desc_cols)
            amount_val = _parse_amount(row[amount_col]) if amount_col is not None else None

            if date_val is None or amount_val is None:
                skipped_rows.append(i)
                continue

            candidates.append(
                {
                    "row_number": i,
                    "date": date_val,
                    "description": desc_val,
                    "amount": amount_val,
                    "original_data": _build_original_data(headers, row),
                }
            )
        except (IndexError, ValueError):
            skipped_rows.append(i)

    file_hash = compute_file_hash(file_path)
    return _result_dict(
        file_path=file_path,
        file_hash=file_hash,
        encoding=encoding,
        candidates=candidates,
        skipped_rows=skipped_rows,
        errors=errors,
    )


def _import_aeon_card_csv(*, file_path: str, encoding: str, rows: list[list[str]]) -> dict:
    """Parse AEON card statements with leading metadata and section headers."""
    candidates = []
    skipped_rows = []
    errors: list[str] = []

    detail_start = None
    detail_headers: list[str] | None = None
    detail_end = len(rows)

    for idx, row in enumerate(rows):
        first = row[0].strip() if row else ""
        if first == "ご利用明細":
            if idx + 1 < len(rows):
                detail_headers = [cell.strip() for cell in rows[idx + 1]]
                detail_start = idx + 2
            break

    if detail_start is None or detail_headers is None:
        file_hash = compute_file_hash(file_path)
        errors.append("AEON card detail section not found")
        return _result_dict(
            file_path=file_path,
            file_hash=file_hash,
            encoding=encoding,
            candidates=[],
            skipped_rows=[],
            errors=errors,
        )

    for idx in range(detail_start, len(rows)):
        row = rows[idx]
        first = row[0].strip() if row else ""
        if first == "分割・ボーナス払い明細":
            detail_end = idx
            break

    date_col = _detect_date_column(detail_headers)
    merchant_col = None
    note_col = None
    amount_col = _detect_amount_column(detail_headers)
    for i, header in enumerate(detail_headers):
        h = header.strip()
        if h == "ご利用先":
            merchant_col = i
        elif h == "備考":
            note_col = i

    for i, row in enumerate(rows[detail_start:detail_end], start=detail_start + 1):
        if not row or all(not cell.strip() for cell in row):
            continue
        if (
            date_col is None
            or merchant_col is None
            or amount_col is None
            or date_col >= len(row)
            or merchant_col >= len(row)
            or amount_col >= len(row)
        ):
            skipped_rows.append(i)
            continue

        date_val = _normalize_date(row[date_col])
        merchant = row[merchant_col].strip()
        amount_val = _parse_amount(row[amount_col])
        note = row[note_col].strip() if note_col is not None and note_col < len(row) else ""

        if date_val is None or not merchant or amount_val is None:
            skipped_rows.append(i)
            continue

        description = merchant
        if note and note not in {"ポイント２倍対象"}:
            description = f"{merchant} {note}"

        candidates.append(
            {
                "row_number": i,
                "date": date_val,
                "description": description,
                "amount": amount_val,
                "original_data": _build_original_data(detail_headers, row),
            }
        )

    file_hash = compute_file_hash(file_path)
    return _result_dict(
        file_path=file_path,
        file_hash=file_hash,
        encoding=encoding,
        candidates=candidates,
        skipped_rows=skipped_rows,
        errors=errors,
    )


def import_csv(*, file_path: str) -> dict:
    """Parse a CSV file and return CSVImportCandidate list.

    Supports UTF-8 and Shift_JIS encoding.
    Does not guess account codes (that is left to Claude/Skills).
    """
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    encoding = _detect_encoding(file_path)
    try:
        text = _read_csv_text(file_path, encoding)
    except Exception as e:
        return {"status": "error", "message": f"Read error: {e}"}

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        return {
            "status": "ok",
            "file_path": file_path,
            "encoding": encoding,
            "total_rows": 0,
            "candidates": [],
            "skipped_rows": [],
            "errors": [],
        }

    fmt = _detect_import_format(rows)
    if fmt == "aeon_card":
        return _import_aeon_card_csv(
            file_path=file_path,
            encoding=encoding,
            rows=rows,
        )
    return _import_generic_csv(file_path=file_path, encoding=encoding, rows=rows)


def import_receipt(*, file_path: str) -> dict:
    """Check file existence and return a ReceiptData template.

    OCR is performed by Claude Vision, so this tool only verifies the file
    exists and returns an empty template for Claude to fill in.
    """
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    return {
        "status": "ok",
        "file_path": file_path,
        "date": None,
        "vendor": None,
        "total_amount": None,
        "items": [],
        "tax_included": True,
    }


def _extract_pdf_text(file_path: str) -> str:
    """Extract text from a PDF using tools/pdf.extract_text()."""
    from shinkoku.tools.pdf import extract_text

    result = extract_text(file_path=file_path)
    if result.get("status") == "ok":
        return result.get("full_text", "")
    return ""


def import_invoice(*, file_path: str) -> dict:
    """請求書の読み取り。PDF の場合はテキスト抽出し、画像の場合は Claude Vision に委任する。"""
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    extracted_text = ""
    if path.suffix.lower() == ".pdf":
        extracted_text = _extract_pdf_text(file_path)

    return {
        "status": "ok",
        "file_path": file_path,
        "extracted_text": extracted_text,
        "vendor": None,
        "invoice_number": None,
        "date": None,
        "total_amount": None,
        "tax_amount": None,
    }


def import_withholding(*, file_path: str) -> dict:
    """源泉徴収票の読み取り。PDF の場合はテキスト抽出し、画像の場合は Claude Vision に委任する。"""
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    extracted_text = ""
    if path.suffix.lower() == ".pdf":
        extracted_text = _extract_pdf_text(file_path)

    return {
        "status": "ok",
        "file_path": file_path,
        "extracted_text": extracted_text,
        "payer_name": None,
        "payment_amount": 0,
        "withheld_tax": 0,
        "social_insurance": 0,
        "life_insurance_deduction": 0,
        "earthquake_insurance_deduction": 0,
        "housing_loan_deduction": 0,
    }


def import_furusato_receipt(*, file_path: str) -> dict:
    """Check receipt file existence and return FurusatoReceiptData template.

    OCR is performed by Claude Vision, so this tool only verifies the file
    exists and returns an empty template for Claude to fill in.
    """
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    return {
        "status": "ok",
        "file_path": file_path,
        "municipality_name": None,
        "municipality_prefecture": None,
        "address": None,
        "amount": None,
        "date": None,
        "receipt_number": None,
    }


def import_payment_statement(*, file_path: str) -> dict:
    """Check payment statement file and return template for data extraction.

    支払調書（報酬、料金、契約金及び賞金の支払調書）の読み取り。
    PDF の場合はテキスト抽出し、画像の場合は Claude Vision に委任する。
    """
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    extracted_text = ""
    if path.suffix.lower() == ".pdf":
        extracted_text = _extract_pdf_text(file_path)

    return {
        "status": "ok",
        "file_path": file_path,
        "extracted_text": extracted_text,
        "payer_name": None,
        "category": None,  # 区分（報酬/料金/契約金等）
        "gross_amount": None,  # 支払金額
        "withholding_tax": None,  # 源泉徴収税額
    }


def import_deduction_certificate(*, file_path: str) -> dict:
    """Check deduction certificate file and return template for OCR.

    控除証明書（生命保険料・地震保険料・社会保険料・小規模企業共済等）の
    読み取りテンプレートを返す。画像の場合は Claude Vision で OCR する。
    """
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    extracted_text = ""
    if path.suffix.lower() == ".pdf":
        extracted_text = _extract_pdf_text(file_path)

    return {
        "status": "ok",
        "file_path": file_path,
        "extracted_text": extracted_text,
        # 以下は Claude Vision / Claude が OCR 結果から埋める
        "certificate_type": None,  # life_insurance / earthquake_insurance / social_insurance / small_business_mutual_aid
        "policy_type": None,  # new / old (生命保険の新旧制度)
        "category": None,  # general / medical_care / annuity (生命保険の区分)
        "company_name": None,  # 保険会社名・機関名
        "policy_number": None,  # 証券番号
        "annual_premium": None,  # 年間保険料（円）
        "is_old_long_term": None,  # 旧長期損害保険かどうか
        "insurance_type": None,  # 社会保険の種別
        "sub_type": None,  # 小規模企業共済の種別（ideco / small_business / disability）
    }


def import_check_csv_imported(*, db_path: str, fiscal_year: int, file_path: str) -> dict:
    """Check if a CSV file has already been imported."""
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    file_hash = compute_file_hash(file_path)
    conn = get_connection(db_path)
    try:
        record = check_source_file_imported(conn, fiscal_year, file_hash)
        if record:
            return {
                "status": "already_imported",
                "file_path": file_path,
                "file_hash": file_hash,
                "import_record": record,
            }
        return {
            "status": "not_imported",
            "file_path": file_path,
            "file_hash": file_hash,
        }
    finally:
        conn.close()


def import_record_source(
    *, db_path: str, fiscal_year: int, file_path: str, row_count: int = 0
) -> dict:
    """Record that a file has been imported."""
    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    file_hash = compute_file_hash(file_path)
    file_name = path.name
    conn = get_connection(db_path)
    try:
        source_id = record_import_source(
            conn,
            fiscal_year,
            file_hash,
            file_name,
            file_path=file_path,
            row_count=row_count,
        )
        return {
            "status": "ok",
            "import_source_id": source_id,
            "file_hash": file_hash,
            "file_name": file_name,
        }
    finally:
        conn.close()
