from __future__ import annotations

from pathlib import Path
from typing import Any
import shlex
import calendar

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from shinkoku.models import JournalSearchParams, JournalEntry
from shinkoku.tools.ledger import (
    ledger_trial_balance,
    ledger_pl,
    ledger_bs,
    ledger_general_ledger,
    ledger_search,
    ledger_add_journals_batch,
    ledger_audit_log,
    ledger_check_duplicates,
)
from shinkoku.db import get_connection
from shinkoku.tools.import_data import import_csv
from fastapi import UploadFile, Form, File
from fastapi.responses import PlainTextResponse, StreamingResponse
import csv as _csv
import io as _io
import os
import base64
import json as _json
from typing import Optional
import mimetypes
import re
from urllib.parse import urlencode

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore


def _templ_env() -> Environment:
    base = Path(__file__).parent
    tpl = base / "templates"
    env = Environment(
        loader=FileSystemLoader(str(tpl)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    # Make Python's range available in templates
    env.globals["range"] = range
    # Add comma formatting filter for integers
    def _comma(v):
        try:
            return f"{int(v):,}"
        except Exception:
            try:
                return f"{float(v):,}"
            except Exception:
                return v
    env.filters["comma"] = _comma
    env.filters["urlencode"] = lambda v: urlencode({"v": v})[2:] if v is not None else ""
    env.globals["set_query_sort"] = _set_query_sort
    return env


def _resolve_source_path(raw_path: str | None, *, base_dir: Path) -> Path | None:
    if not raw_path:
        return None
    expanded = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    candidate = expanded if expanded.is_absolute() else (base_dir / expanded)
    try:
        return candidate.resolve(strict=False)
    except Exception:
        return candidate


def _source_file_aliases(raw_path: str | None, *, base_dir: Path) -> list[str]:
    """Build exact-match aliases for the same source file across Windows/WSL paths."""
    if not raw_path:
        return []

    aliases: list[str] = []

    def _add(value: str | None) -> None:
        if value and value not in aliases:
            aliases.append(value)

    _add(raw_path)

    resolved = _resolve_source_path(raw_path, base_dir=base_dir)
    if resolved is not None:
        _add(str(resolved))

    path_text = raw_path.replace("/", "\\")
    m = re.match(r"^([A-Za-z]):\\(.*)$", path_text)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace("\\", "/")
        _add(f"/mnt/{drive}/{rest}")

    m = re.match(r"^/mnt/([a-zA-Z])/(.*)$", raw_path)
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).replace("/", "\\")
        _add(f"{drive}:\\{rest}")

    return aliases


def _safe_relpath(path: Path, *, base_dir: Path) -> str:
    try:
        return str(path.relative_to(base_dir))
    except Exception:
        return str(path)


def _preview_source_file(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        return {"kind": "image"}
    if suffix == ".pdf":
        return {"kind": "pdf"}
    if suffix in {".csv", ".txt", ".log", ".json", ".yaml", ".yml", ".md"}:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = path.read_text(encoding="shift_jis")
            except Exception:
                text = path.read_text(errors="replace")
        if suffix == ".csv":
            reader = _csv.reader(_io.StringIO(text))
            rows: list[list[str]] = []
            for idx, row in enumerate(reader):
                if idx >= 12:
                    break
                rows.append(row[:8])
            return {"kind": "csv", "rows": rows}
        return {"kind": "text", "text": text[:4000]}
    return {"kind": "binary"}


def _build_journals_url(**params: Any) -> str:
    query = {key: value for key, value in params.items() if value not in (None, "", False)}
    return "/journals" + (f"?{urlencode(query)}" if query else "")


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _blank_to_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    return int(stripped)


def _quote_query_token(token: str) -> str:
    if not token:
        return token
    if any(ch.isspace() for ch in token) or '"' in token:
        escaped = token.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return token


def _set_query_sort(raw_query: str | None, field: str, direction: str) -> str:
    try:
        tokens = shlex.split(raw_query or "")
    except ValueError:
        tokens = (raw_query or "").split()

    kept: list[str] = []
    for token in tokens:
        lower = token.lower()
        if lower in {"asc", "desc"}:
            continue
        if lower.startswith("sort:"):
            continue
        kept.append(token)

    kept.append(f"sort:{field}")
    kept.append(direction)
    return " ".join(_quote_query_token(token) for token in kept if token)


def _parse_journal_query(raw_query: str | None) -> dict[str, Any]:
    parsed: dict[str, Any] = {
        "free_terms": [],
        "exclude_terms": [],
        "description_terms": [],
        "counterparty_terms": [],
        "q": None,
        "account_code": None,
        "category": None,
        "counterparty": None,
        "source": None,
        "source_file": None,
        "date_from": None,
        "date_to": None,
        "amount_min": None,
        "amount_max": None,
        "sort": None,
        "dir": None,
    }
    if not raw_query:
        return parsed

    try:
        tokens = shlex.split(raw_query)
    except ValueError:
        tokens = raw_query.split()

    for token in tokens:
        lower = token.lower()
        if lower in {"asc", "desc"}:
            parsed["dir"] = lower
            continue
        if ":" in token:
            key, value = token.split(":", 1)
            key = key.lower()
            if not value:
                continue
            if key in {"q", "query"}:
                parsed["free_terms"].append(value)
            elif key == "desc":
                parsed["description_terms"].append(value)
            elif key == "cp":
                parsed["counterparty_terms"].append(value)
            elif key == "acc":
                parsed["account_code"] = value
            elif key == "cat":
                parsed["category"] = value.lower()
            elif key == "src":
                parsed["source"] = value
            elif key == "file":
                parsed["source_file"] = value
            elif key == "from":
                parsed["date_from"] = value
            elif key == "to":
                parsed["date_to"] = value
            elif key == "mfrom":
                parsed["amount_min"] = value
            elif key == "mto":
                parsed["amount_max"] = value
            elif key == "sort":
                parsed["sort"] = value
            continue
        if token.startswith("-") and len(token) > 1:
            parsed["exclude_terms"].append(token[1:])
            continue
        parsed["free_terms"].append(token)

    if parsed["free_terms"]:
        parsed["q"] = " ".join(parsed["free_terms"])
    return parsed


def _journal_search_text(journal: dict[str, Any], account_names: dict[str, str]) -> str:
    parts: list[str] = [
        str(journal.get("id") or ""),
        str(journal.get("date") or ""),
        str(journal.get("description") or ""),
        str(journal.get("counterparty") or ""),
        str(journal.get("source") or ""),
        str(journal.get("source_file") or ""),
    ]
    for line in journal.get("lines", []):
        code = str(line.get("account_code") or "")
        parts.append(code)
        parts.append(account_names.get(code, ""))
    return " ".join(parts).lower()


def _filter_journals_by_query(
    journals: list[dict[str, Any]],
    *,
    free_terms: list[str],
    exclude_terms: list[str],
    description_terms: list[str],
    counterparty_terms: list[str],
    account_names: dict[str, str],
) -> list[dict[str, Any]]:
    if not free_terms and not exclude_terms and not description_terms and not counterparty_terms:
        return journals

    free_terms_l = [term.lower() for term in free_terms if term]
    exclude_terms_l = [term.lower() for term in exclude_terms if term]
    description_terms_l = [term.lower() for term in description_terms if term]
    counterparty_terms_l = [term.lower() for term in counterparty_terms if term]
    filtered: list[dict[str, Any]] = []
    for journal in journals:
        haystack = _journal_search_text(journal, account_names)
        description = str(journal.get("description") or "").lower()
        counterparty = str(journal.get("counterparty") or "").lower()
        if free_terms_l and not all(term in haystack for term in free_terms_l):
            continue
        if description_terms_l and not all(term in description for term in description_terms_l):
            continue
        if counterparty_terms_l and not all(term in counterparty for term in counterparty_terms_l):
            continue
        if exclude_terms_l and any(term in haystack for term in exclude_terms_l):
            continue
        filtered.append(journal)
    return filtered


def _sum_journal_debits(journals: list[dict[str, Any]]) -> int:
    return sum(
        sum(int(line.get("amount") or 0) for line in journal.get("lines", []) if line.get("side") == "debit")
        for journal in journals
    )


def create_app(*, db_path: str, fiscal_year: int) -> FastAPI:
    app = FastAPI(title="shinkoku Web UI")

    app.state.db_path = db_path
    app.state.fiscal_year = fiscal_year
    app.state.base_dir = Path(db_path).resolve().parent
    env = _templ_env()

    # static (for future extension)
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/favicon.ico")
    def favicon() -> HTMLResponse:
        # Avoid 404 noise; return no content
        return HTMLResponse(status_code=204)

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse(url="/journals", status_code=307)

    @app.get("/trial-balance", response_class=HTMLResponse)
    def trial_balance(
        request: Request,
        q: str | None = None,
        category: str | None = None,
        order: str | None = None,
        dir: str | None = None,
    ) -> str:
        tb = ledger_trial_balance(db_path=app.state.db_path, fiscal_year=app.state.fiscal_year)
        accounts = list(tb.get("accounts", [])) if isinstance(tb, dict) else []

        if q:
            ql = q.lower()
            accounts = [
                a for a in accounts
                if ql in str(a.get("account_code", "")).lower()
                or ql in str(a.get("account_name", "")).lower()
            ]
        if category:
            accounts = [a for a in accounts if str(a.get("category", "")) == category]

        order = order or "amount"
        dir = dir or "desc"
        reverse = dir != "asc"
        if order == "count":
            sort_key = lambda a: (
                1 if int(a.get("debit_total", 0) or 0) != 0 or int(a.get("credit_total", 0) or 0) != 0 else 0
            )
        elif order == "name":
            sort_key = lambda a: str(a.get("account_name", ""))
            reverse = dir == "desc"
        elif order == "code":
            sort_key = lambda a: str(a.get("account_code", ""))
            reverse = dir == "desc"
        else:
            sort_key = lambda a: abs(int(a.get("balance", 0) or 0))
        accounts = sorted(accounts, key=sort_key, reverse=reverse)

        tb_view = dict(tb) if isinstance(tb, dict) else {"accounts": []}
        tb_view["accounts"] = accounts
        template = env.get_template("trial_balance.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            tb=tb_view,
            q=q or "",
            category=category or "",
            order=order,
            dir=dir,
            total_groups=len(accounts),
        )

    @app.get("/bs", response_class=HTMLResponse)
    def bs_full(request: Request) -> str:
        bs = ledger_bs(db_path=app.state.db_path, fiscal_year=app.state.fiscal_year)
        # 並び替え（降順）
        if isinstance(bs, dict):
            bs["assets"] = sorted(bs.get("assets", []), key=lambda x: x.get("amount", 0), reverse=True)
            bs["liabilities"] = sorted(bs.get("liabilities", []), key=lambda x: x.get("amount", 0), reverse=True)
            bs["equity"] = sorted(bs.get("equity", []), key=lambda x: x.get("amount", 0), reverse=True)
        template = env.get_template("bs.html")
        return template.render(request=request, fiscal_year=app.state.fiscal_year, bs=bs)

    @app.get("/pl", response_class=HTMLResponse)
    def pl_full(request: Request) -> str:
        pl = ledger_pl(db_path=app.state.db_path, fiscal_year=app.state.fiscal_year)
        # 並び替え（降順）
        if isinstance(pl, dict):
            pl["revenues"] = sorted(pl.get("revenues", []), key=lambda x: x.get("amount", 0), reverse=True)
            pl["expenses"] = sorted(pl.get("expenses", []), key=lambda x: x.get("amount", 0), reverse=True)
        template = env.get_template("pl.html")
        return template.render(request=request, fiscal_year=app.state.fiscal_year, pl=pl)

    @app.get("/journals", response_class=HTMLResponse)
    def journals(
        request: Request,
        query: str | None = None,
        q: str | None = None,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        account_code: str | None = None,
        counterparty: str | None = None,
        source_file: str | None = None,
        amount_min: str | None = None,
        amount_max: str | None = None,
        limit: int = 100,
        offset: int = 0,
        show_source: int = 0,
        sort: str | None = None,
        dir: str | None = None,
    ) -> str:
        try:
            counterparty = _blank_to_none(counterparty)
            source_file = _blank_to_none(source_file)
            source = _blank_to_none(source)
            date_from = _blank_to_none(date_from)
            date_to = _blank_to_none(date_to)
            account_code = _blank_to_none(account_code)
            parsed_query = _parse_journal_query(query)
            amount_min_value = _blank_to_int(parsed_query["amount_min"]) if query else _blank_to_int(amount_min)
            amount_max_value = _blank_to_int(parsed_query["amount_max"]) if query else _blank_to_int(amount_max)
            effective_q = parsed_query["q"] if query else q
            effective_source = parsed_query["source"] or source if query else source
            effective_date_from = parsed_query["date_from"] or date_from if query else date_from
            effective_date_to = parsed_query["date_to"] or date_to if query else date_to
            effective_account_code = parsed_query["account_code"] or account_code if query else account_code
            effective_category = parsed_query["category"] if query else None
            effective_counterparty = counterparty
            effective_source_file = parsed_query["source_file"] or source_file if query else source_file
            effective_sort = parsed_query["sort"] or sort if query else sort
            effective_dir = parsed_query["dir"] or dir if query else dir
            fetch_limit = 5000 if query else limit
            fetch_offset = 0 if query else offset

            params = JournalSearchParams(
                fiscal_year=app.state.fiscal_year,
                date_from=effective_date_from,
                date_to=effective_date_to,
                account_code=effective_account_code,
                category=effective_category,
                description_contains=effective_q if not query else None,
                counterparty_contains=effective_counterparty,
                amount_min=amount_min_value,
                amount_max=amount_max_value,
                source=effective_source,
                source_file=effective_source_file,
                limit=fetch_limit,
                offset=fetch_offset,
            )
            result = ledger_search(db_path=app.state.db_path, params=params)

            names: dict[str, str] = {}
            conn = get_connection(app.state.db_path)
            try:
                for code, name in conn.execute("SELECT code, name FROM accounts").fetchall():
                    names[str(code)] = name
            finally:
                conn.close()

            if query and isinstance(result, dict) and isinstance(result.get("journals"), list):
                journals_all = _filter_journals_by_query(
                    result["journals"],
                    free_terms=parsed_query["free_terms"],
                    exclude_terms=parsed_query["exclude_terms"],
                    description_terms=parsed_query["description_terms"],
                    counterparty_terms=parsed_query["counterparty_terms"],
                    account_names=names,
                )
                result["total_count"] = len(journals_all)
                result["journals"] = journals_all[offset : offset + limit]
            else:
                journals_all = None

            sort_key = None
            if effective_sort in {"id", "date", "source_file"}:
                if effective_sort == "id":
                    sort_key = lambda j: j.get("id", 0)
                elif effective_sort == "date":
                    sort_key = lambda j: j.get("date", "")
                elif effective_sort == "source_file":
                    sort_key = lambda j: (j.get("source_file") or "")
            if sort_key and isinstance(result, dict) and isinstance(result.get("journals"), list):
                result["journals"] = sorted(
                    result["journals"],
                    key=sort_key,
                    reverse=(effective_dir == "desc"),
                )

            has_filters = bool(
                query
                or effective_q
                or effective_source
                or effective_date_from
                or effective_date_to
                or effective_account_code
                or effective_category
                or effective_counterparty
                or effective_source_file
                or amount_min_value is not None
                or amount_max_value is not None
            )
            total_amount = 0
            if has_filters:
                if journals_all is not None:
                    total_amount = _sum_journal_debits(journals_all)
                elif isinstance(result, dict) and int(result.get("total_count", 0) or 0) > 0:
                    total_params = params.model_copy(update={"limit": int(result["total_count"]), "offset": 0})
                    total_result = ledger_search(db_path=app.state.db_path, params=total_params)
                    total_amount = _sum_journal_debits(total_result.get("journals", []))

            context = dict(
                request=request,
                fiscal_year=app.state.fiscal_year,
                view_title="仕訳一覧",
                query=query or "",
                q="" if query else (effective_q or ""),
                source=effective_source or "",
                date_from=effective_date_from or "",
                date_to=effective_date_to or "",
                account_code=effective_account_code or "",
                category=effective_category or "",
                counterparty=effective_counterparty or "",
                source_file=effective_source_file or "",
                amount_min="" if amount_min_value is None else amount_min_value,
                amount_max="" if amount_max_value is None else amount_max_value,
                limit=limit,
                offset=offset,
                res=result,
                total_amount=total_amount,
                show_source=bool(show_source),
                account_names=names,
                sort=effective_sort or "",
                dir=effective_dir or "",
                active_filters=[
                    label
                    for label in [
                        f"検索式: {query}" if query else "",
                        f"摘要条件: {' / '.join(parsed_query['description_terms'])}" if parsed_query["description_terms"] else "",
                        f"取引先条件: {' / '.join(parsed_query['counterparty_terms'])}" if parsed_query["counterparty_terms"] else "",
                        f"勘定科目: {effective_account_code} {names.get(effective_account_code or '', '')}".strip() if effective_account_code else "",
                        f"区分: {effective_category}（{ {'asset': '資産', 'liability': '負債', 'equity': '純資産', 'revenue': '売上', 'expense': '費用'}.get(effective_category, effective_category) }）" if effective_category else "",
                        f"取引先: {effective_counterparty}" if effective_counterparty else "",
                        f"入力元: {effective_source_file}" if effective_source_file else "",
                        f"金額下限: {amount_min_value:,}円" if amount_min_value is not None else "",
                        f"金額上限: {amount_max_value:,}円" if amount_max_value is not None else "",
                        f"検索: {effective_q}" if effective_q and not query else "",
                        f"source: {effective_source}" if effective_source else "",
                        f"期間: {effective_date_from or '開始日なし'} - {effective_date_to or '終了日なし'}" if (effective_date_from or effective_date_to) else "",
                    ]
                    if label
                ],
            )
            context["show_total_amount"] = has_filters
            if request.headers.get("HX-Request"):
                template = env.get_template("partials/journals_table.html")
                return template.render(**context)
            template = env.get_template("journals.html")
            return template.render(**context)
        except Exception as e:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(f"/journals error: {e}", status_code=500)

    @app.get("/health")
    def health() -> dict[str, Any]:
        # Simple diagnostics: DB path and FY + counts
        info: dict[str, Any] = {
            "status": "ok",
            "db_path": app.state.db_path,
            "fiscal_year": app.state.fiscal_year,
        }
        try:
            conn = get_connection(app.state.db_path)
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM journals WHERE fiscal_year = ?",
                    (app.state.fiscal_year,),
                )
                info["journals_in_fy"] = cur.fetchone()[0]
            finally:
                conn.close()
        except Exception:
            pass
        return info

    @app.get("/debug/routes")
    def debug_routes() -> list[str]:
        return [r.path for r in app.router.routes]

    @app.get("/gl/{account_code}", response_class=HTMLResponse)
    def general_ledger(request: Request, account_code: str) -> str:
        res = ledger_general_ledger(
            db_path=app.state.db_path,
            fiscal_year=app.state.fiscal_year,
            account_code=account_code,
        )
        template = env.get_template("gl.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            account_code=account_code,
            gl=res,
        )

    # ========== Duplicate Candidates ==========
    @app.get("/duplicates", response_class=HTMLResponse)
    def duplicates(request: Request, threshold: int = 70) -> str:
        res = ledger_check_duplicates(db_path=app.state.db_path, fiscal_year=app.state.fiscal_year, threshold=threshold)
        pairs: list[dict] = res.get("pairs", []) if isinstance(res, dict) else []
        # Fetch minimal info for each journal in pairs
        ids: set[int] = set()
        for p in pairs:
            ids.add(int(p.get("journal_id_a")))
            ids.add(int(p.get("journal_id_b")))
        meta: dict[int, dict] = {}
        if ids:
            conn = get_connection(app.state.db_path)
            try:
                placeholders = ",".join(["?"] * len(ids))
                rows = conn.execute(
                    "SELECT j.id, j.date, COALESCE(j.description,''), j.counterparty, j.source, j.source_file, "
                    "SUM(CASE WHEN jl.side='debit' THEN jl.amount ELSE 0 END) AS debit_sum, "
                    "SUM(CASE WHEN jl.side='credit' THEN jl.amount ELSE 0 END) AS credit_sum "
                    "FROM journals j INNER JOIN journal_lines jl ON jl.journal_id = j.id "
                    f"WHERE j.id IN ({placeholders}) GROUP BY j.id",
                    list(ids),
                ).fetchall()
                for r in rows:
                    meta[int(r[0])] = {
                        "id": r[0],
                        "date": r[1],
                        "description": r[2],
                        "counterparty": r[3],
                        "source": r[4],
                        "source_file": r[5],
                        "debit": r[6] or 0,
                        "credit": r[7] or 0,
                    }
            finally:
                conn.close()
        # Build view models
        items: list[dict] = []
        for p in pairs:
            a_id = int(p.get("journal_id_a"))
            b_id = int(p.get("journal_id_b"))
            items.append(
                {
                    "a": meta.get(a_id, {"id": a_id}),
                    "b": meta.get(b_id, {"id": b_id}),
                    "score": p.get("score", 0),
                    "reason": p.get("reason", ""),
                }
            )
        template = env.get_template("duplicates.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            threshold=threshold,
            exact_count=res.get("exact_count", 0),
            suspected_count=res.get("suspected_count", 0),
            items=items,
        )

    # ========== Description Summary ==========
    def _build_filters(where: list[str], params: list, *, source: str | None, date_from: str | None, date_to: str | None) -> None:
        if source:
            where.append("j.source = ?")
            params.append(source)
        if date_from:
            where.append("j.date >= ?")
            params.append(date_from)
        if date_to:
            where.append("j.date <= ?")
            params.append(date_to)

    @app.get("/summary/description", response_class=HTMLResponse)
    def summary_description(
        request: Request,
        q: str | None = None,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order: str | None = None,  # 'amount'|'count'|'name'
        dir: str | None = None,    # 'asc'|'desc'
    ) -> str:
        where = ["j.fiscal_year = ?"]
        params: list = [app.state.fiscal_year]
        if q:
            where.append("j.description LIKE ?")
            params.append(f"%{q}%")
        _build_filters(where, params, source=source, date_from=date_from, date_to=date_to)
        where_clause = " AND ".join(where)

        # Order
        order_by = "total_amount DESC"
        if order == "count":
            order_by = f"journal_count {'DESC' if dir=='desc' or dir is None else 'ASC'}"
        elif order == "name":
            order_by = f"description {'ASC' if dir!='desc' else 'DESC'}"
        elif order == "amount":
            order_by = f"total_amount {'DESC' if dir=='desc' or dir is None else 'ASC'}"

        sql = (
            "SELECT COALESCE(j.description, '') AS description, "
            "COUNT(DISTINCT j.id) AS journal_count, "
            "SUM(CASE WHEN jl.side='debit' THEN jl.amount ELSE 0 END) AS total_amount "
            "FROM journals j INNER JOIN journal_lines jl ON jl.journal_id = j.id "
            f"WHERE {where_clause} "
            "GROUP BY COALESCE(j.description, '') "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?"
        )
        conn = get_connection(app.state.db_path)
        try:
            rows = conn.execute(sql, params + [limit, offset]).fetchall()
            # total count for pagination
            count_sql = (
                "SELECT COUNT(*) FROM (SELECT 1 FROM journals j "
                f"WHERE {where_clause} GROUP BY COALESCE(j.description,'')"
                ") t"
            )
            total_groups = conn.execute(count_sql, params).fetchone()[0]
            groups = [
                {
                    "description": r[0],
                    "journal_count": r[1],
                    "total_amount": r[2] or 0,
                }
                for r in rows
            ]
        finally:
            conn.close()

        template = env.get_template("summary_description.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            groups=groups,
            total_groups=total_groups,
            q=q or "",
            source=source or "",
            date_from=date_from or "",
            date_to=date_to or "",
            limit=limit,
            offset=offset,
            order=order or "amount",
            dir=dir or "desc",
        )

    @app.get("/summary/description/detail", response_class=HTMLResponse)
    def summary_description_detail(
        request: Request,
        desc: str,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> str:
        return RedirectResponse(
            url=_build_journals_url(
                query=f'desc:"{desc}"',
                source=source,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                offset=offset,
            ),
            status_code=302,
        )

    # ========== Monthly Summary (PL) ==========
    @app.get("/summary/monthly", response_class=HTMLResponse)
    def summary_monthly(
        request: Request,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> str:
        where = ["j.fiscal_year = ?"]
        params: list = [app.state.fiscal_year]
        _build_filters(where, params, source=source, date_from=date_from, date_to=date_to)
        where_clause = " AND ".join(where)
        sql = (
            "SELECT substr(j.date,1,7) AS ym, "
            "SUM(CASE WHEN a.category='revenue' AND jl.side='credit' THEN jl.amount "
            "         WHEN a.category='revenue' AND jl.side='debit' THEN -jl.amount ELSE 0 END) AS revenue, "
            "SUM(CASE WHEN a.category='expense' AND jl.side='debit' THEN jl.amount "
            "         WHEN a.category='expense' AND jl.side='credit' THEN -jl.amount ELSE 0 END) AS expense "
            "FROM journals j INNER JOIN journal_lines jl ON jl.journal_id = j.id "
            "INNER JOIN accounts a ON a.code = jl.account_code "
            f"WHERE {where_clause} GROUP BY ym ORDER BY ym"
        )
        conn = get_connection(app.state.db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
            balance_where = ["j.fiscal_year = ?"]
            balance_params: list = [app.state.fiscal_year]
            if source:
                balance_where.append("j.source = ?")
                balance_params.append(source)
            balance_where_clause = " AND ".join(balance_where)

            opening_rows = conn.execute(
                "SELECT a.category, COALESCE(SUM(ob.amount), 0) "
                "FROM opening_balances ob "
                "INNER JOIN accounts a ON a.code = ob.account_code "
                "WHERE ob.fiscal_year = ? AND a.category IN ('asset', 'liability', 'equity') "
                "GROUP BY a.category",
                (app.state.fiscal_year,),
            ).fetchall()
            opening_totals = {str(r[0]): int(r[1] or 0) for r in opening_rows}

            monthly_balance_rows = conn.execute(
                "SELECT substr(j.date,1,7) AS ym, "
                "SUM(CASE WHEN a.category='asset' AND jl.side='debit' THEN jl.amount "
                "         WHEN a.category='asset' AND jl.side='credit' THEN -jl.amount ELSE 0 END) AS asset_delta, "
                "SUM(CASE WHEN a.category='liability' AND jl.side='credit' THEN jl.amount "
                "         WHEN a.category='liability' AND jl.side='debit' THEN -jl.amount ELSE 0 END) AS liability_delta, "
                "SUM(CASE WHEN a.category='equity' AND jl.side='credit' THEN jl.amount "
                "         WHEN a.category='equity' AND jl.side='debit' THEN -jl.amount ELSE 0 END) AS equity_delta "
                "FROM journals j "
                "INNER JOIN journal_lines jl ON jl.journal_id = j.id "
                "INNER JOIN accounts a ON a.code = jl.account_code "
                f"WHERE {balance_where_clause} AND a.category IN ('asset', 'liability', 'equity') "
                "GROUP BY ym ORDER BY ym",
                balance_params,
            ).fetchall()
            cumulative_balances: dict[str, dict[str, int]] = {}
            asset_balance = int(opening_totals.get("asset", 0))
            liability_balance = int(opening_totals.get("liability", 0))
            equity_balance = int(opening_totals.get("equity", 0))
            for r in monthly_balance_rows:
                ym = str(r[0] or "")
                asset_balance += int(r[1] or 0)
                liability_balance += int(r[2] or 0)
                equity_balance += int(r[3] or 0)
                cumulative_balances[ym] = {
                    "asset_balance": asset_balance,
                    "liability_balance": liability_balance,
                    "equity_balance": equity_balance,
                }

            items = []
            for r in rows:
                ym = r[0]
                revenue = int(r[1] or 0)
                expense = int(r[2] or 0)
                net = revenue - expense
                year = int(ym[:4])
                month = int(ym[5:7])
                month_end = calendar.monthrange(year, month)[1]
                balances = cumulative_balances.get(
                    ym,
                    {
                        "asset_balance": int(opening_totals.get("asset", 0)),
                        "liability_balance": int(opening_totals.get("liability", 0)),
                        "equity_balance": int(opening_totals.get("equity", 0)),
                    },
                )
                items.append(
                    {
                        "ym": ym,
                        "month_from": f"{ym}-01",
                        "month_to": f"{ym}-{month_end:02d}",
                        "revenue": revenue,
                        "expense": expense,
                        "net": net,
                        "asset_balance": balances["asset_balance"],
                        "liability_balance": balances["liability_balance"],
                        "equity_balance": balances["equity_balance"],
                    }
                )
            # totals
            totals = {
                "revenue": sum(i["revenue"] for i in items),
                "expense": sum(i["expense"] for i in items),
                "net": sum(i["net"] for i in items),
            }
        finally:
            conn.close()
        template = env.get_template("summary_monthly.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            items=items,
            totals=totals,
            source=source or "",
            date_from=date_from or "",
            date_to=date_to or "",
        )

    # ========== Tax Summary ==========
    @app.get("/summary/tax", response_class=HTMLResponse)
    def summary_tax(
        request: Request,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> str:
        where = ["j.fiscal_year = ?"]
        params: list = [app.state.fiscal_year]
        _build_filters(where, params, source=source, date_from=date_from, date_to=date_to)
        where_clause = " AND ".join(where)
        sql = (
            "SELECT COALESCE(jl.tax_category,'(なし)') AS cat, "
            "COUNT(*) AS line_count, "
            "SUM(CASE WHEN jl.side='debit' THEN jl.amount ELSE 0 END) AS debit_total, "
            "SUM(CASE WHEN jl.side='credit' THEN jl.amount ELSE 0 END) AS credit_total, "
            "SUM(COALESCE(jl.tax_amount,0)) AS tax_total "
            "FROM journals j INNER JOIN journal_lines jl ON jl.journal_id = j.id "
            f"WHERE {where_clause} GROUP BY cat ORDER BY tax_total DESC"
        )
        conn = get_connection(app.state.db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
            groups = [
                {
                    "tax_category": r[0],
                    "line_count": r[1] or 0,
                    "debit_total": r[2] or 0,
                    "credit_total": r[3] or 0,
                    "tax_total": r[4] or 0,
                }
                for r in rows
            ]
        finally:
            conn.close()
        template = env.get_template("summary_tax.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            groups=groups,
            source=source or "",
            date_from=date_from or "",
            date_to=date_to or "",
        )

    # ========== Cashbook ==========
    # removed cashbook routes
    def cashbook_index(request: Request) -> RedirectResponse:
        return RedirectResponse(url="/journals", status_code=307)
        # Suggest accounts whose name includes 現金/預金
        conn = get_connection(app.state.db_path)
        try:
            rows = conn.execute(
                "SELECT code, name FROM accounts WHERE is_active=1 AND (name LIKE '%現金%' OR name LIKE '%預金%') ORDER BY sort_order, code"
            ).fetchall()
            accounts = [{"code": r[0], "name": r[1]} for r in rows]
        finally:
            conn.close()
        template = env.get_template("cashbook_index.html")
        return template.render(request=request, fiscal_year=app.state.fiscal_year, accounts=accounts)

    @app.get("/cashbook/{account_code}", response_class=HTMLResponse)
    def cashbook_view(request: Request, account_code: str) -> str:
        return RedirectResponse(url=f"/gl/{account_code}", status_code=307)
        gl = ledger_general_ledger(
            db_path=app.state.db_path,
            fiscal_year=app.state.fiscal_year,
            account_code=account_code,
        )
        template = env.get_template("cashbook.html")
        return template.render(request=request, fiscal_year=app.state.fiscal_year, account_code=account_code, gl=gl)

    @app.get("/source-links", response_class=HTMLResponse)
    def source_links(request: Request, q: str | None = None, only_missing: int = 0) -> str:
        conn = get_connection(app.state.db_path)
        try:
            rows = conn.execute(
                "WITH journal_base AS ("
                "  SELECT j.id, j.fiscal_year, j.source_file, j.source, j.date, "
                "         SUM(CASE WHEN jl.side='debit' THEN jl.amount ELSE 0 END) AS debit_sum "
                "  FROM journals j "
                "  INNER JOIN journal_lines jl ON jl.journal_id = j.id "
                "  WHERE j.fiscal_year = ? AND j.source_file IS NOT NULL AND j.source_file <> '' "
                "  GROUP BY j.id, j.fiscal_year, j.source_file, j.source, j.date"
                ") "
                "SELECT jb.source_file, jb.source, COUNT(DISTINCT jb.id) AS journal_count, "
                "MIN(jb.date) AS first_date, MAX(jb.date) AS last_date, "
                "SUM(jb.debit_sum) AS total_amount, MAX(COALESCE(i.imported_at, '')) AS imported_at "
                "FROM journal_base jb "
                "LEFT JOIN import_sources i "
                "ON i.fiscal_year = jb.fiscal_year "
                "AND (i.file_path = jb.source_file OR jb.source_file LIKE '%' || i.file_name) "
                "GROUP BY jb.source_file, jb.source "
                "ORDER BY last_date DESC, journal_count DESC, jb.source_file",
                (app.state.fiscal_year,),
            ).fetchall()
            items: list[dict[str, Any]] = []
            for r in rows:
                raw_path = r[0]
                resolved = _resolve_source_path(raw_path, base_dir=app.state.base_dir)
                exists = bool(resolved and resolved.exists())
                item = {
                    "source_file": raw_path,
                    "source": r[1] or "",
                    "journal_count": int(r[2] or 0),
                    "first_date": r[3] or "",
                    "last_date": r[4] or "",
                    "total_amount": int(r[5] or 0),
                    "imported_at": r[6] or "",
                    "exists": exists,
                    "resolved_path": str(resolved) if resolved else "",
                    "display_path": _safe_relpath(resolved, base_dir=app.state.base_dir) if resolved else (raw_path or ""),
                    "size": resolved.stat().st_size if exists else None,
                    "file_uri": resolved.as_uri() if exists else "",
                }
                if q:
                    ql = q.lower()
                    hay = " ".join(
                        [
                            str(item["source_file"]),
                            str(item["source"]),
                            str(item["resolved_path"]),
                        ]
                    ).lower()
                    if ql not in hay:
                        continue
                if only_missing and exists:
                    continue
                items.append(item)
        finally:
            conn.close()
        template = env.get_template("source_links.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            items=items,
            q=q or "",
            only_missing=bool(only_missing),
            total_files=len(items),
            total_amount=sum(int(it["total_amount"]) for it in items),
            existing_files=sum(1 for it in items if it["exists"]),
            missing_files=sum(1 for it in items if not it["exists"]),
        )

    @app.get("/source-links/detail", response_class=HTMLResponse)
    def source_link_detail(request: Request, path: str) -> str:
        resolved = _resolve_source_path(path, base_dir=app.state.base_dir)
        exists = bool(resolved and resolved.exists())
        preview = _preview_source_file(resolved) if exists and resolved else {"kind": "missing"}
        conn = get_connection(app.state.db_path)
        try:
            aliases = _source_file_aliases(path, base_dir=app.state.base_dir)
            if not aliases:
                aliases = [path]
            placeholders = ", ".join("?" for _ in aliases)
            rows = conn.execute(
                "SELECT j.id, j.date, COALESCE(j.description,''), j.counterparty, j.source, j.source_file, "
                "SUM(CASE WHEN jl.side='debit' THEN jl.amount ELSE 0 END) AS debit_sum "
                "FROM journals j "
                "INNER JOIN journal_lines jl ON jl.journal_id = j.id "
                f"WHERE j.fiscal_year = ? AND j.source_file IN ({placeholders}) "
                "GROUP BY j.id ORDER BY j.date, j.id",
                (app.state.fiscal_year, *aliases),
            ).fetchall()
            items = [
                {
                    "id": r[0],
                    "date": r[1],
                    "description": r[2],
                    "counterparty": r[3],
                    "source": r[4],
                    "source_file": r[5],
                    "amount": r[6] or 0,
                }
                for r in rows
            ]
            monthly_rows = conn.execute(
                "SELECT substr(j.date, 1, 7) AS ym, "
                "SUM(CASE WHEN jl.side='debit' THEN jl.amount ELSE 0 END) AS debit_sum, "
                "COUNT(DISTINCT j.id) AS journal_count "
                "FROM journals j "
                "INNER JOIN journal_lines jl ON jl.journal_id = j.id "
                f"WHERE j.fiscal_year = ? AND j.source_file IN ({placeholders}) "
                "GROUP BY substr(j.date, 1, 7) "
                "ORDER BY ym",
                (app.state.fiscal_year, *aliases),
            ).fetchall()
            monthly_items = [
                {
                    "month": r[0] or "",
                    "amount": int(r[1] or 0),
                    "journal_count": int(r[2] or 0),
                }
                for r in monthly_rows
            ]
        finally:
            conn.close()
        template = env.get_template("source_link_detail.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            raw_path=path,
            resolved_path=str(resolved) if resolved else "",
            display_path=_safe_relpath(resolved, base_dir=app.state.base_dir) if resolved else path,
            exists=exists,
            file_uri=resolved.as_uri() if exists and resolved else "",
            size=resolved.stat().st_size if exists and resolved else None,
            preview=preview,
            items=items,
            total_amount=sum(int(it["amount"]) for it in items),
            monthly_items=monthly_items,
        )

    @app.get("/source-links/raw")
    def source_link_raw(path: str):
        resolved = _resolve_source_path(path, base_dir=app.state.base_dir)
        if not resolved or not resolved.exists() or not resolved.is_file():
            return PlainTextResponse("source file not found", status_code=404)
        media_type, _ = mimetypes.guess_type(str(resolved))
        return FileResponse(str(resolved), media_type=media_type or "application/octet-stream")

    # ========== Counterparty Summary ==========
    @app.get("/summary/counterparty", response_class=HTMLResponse)
    def summary_counterparty(
        request: Request,
        q: str | None = None,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
        offset: int = 0,
        order: str | None = None,  # 'amount'|'count'|'name'
        dir: str | None = None,    # 'asc'|'desc'
    ) -> str:
        where = ["j.fiscal_year = ?"]
        params: list = [app.state.fiscal_year]
        if q:
            where.append("COALESCE(j.counterparty,'') LIKE ?")
            params.append(f"%{q}%")
        _build_filters(where, params, source=source, date_from=date_from, date_to=date_to)
        where_clause = " AND ".join(where)

        order_by = "total_amount DESC"
        if order == "count":
            order_by = f"journal_count {'DESC' if dir=='desc' or dir is None else 'ASC'}"
        elif order == "name":
            order_by = f"counterparty {'ASC' if dir!='desc' else 'DESC'}"
        elif order == "amount":
            order_by = f"total_amount {'DESC' if dir=='desc' or dir is None else 'ASC'}"

        sql = (
            "SELECT COALESCE(j.counterparty, '') AS counterparty, "
            "COUNT(DISTINCT j.id) AS journal_count, "
            "SUM(CASE WHEN jl.side='debit' THEN jl.amount ELSE 0 END) AS total_amount "
            "FROM journals j INNER JOIN journal_lines jl ON jl.journal_id = j.id "
            f"WHERE {where_clause} "
            "GROUP BY COALESCE(j.counterparty, '') "
            f"ORDER BY {order_by} LIMIT ? OFFSET ?"
        )
        conn = get_connection(app.state.db_path)
        try:
            rows = conn.execute(sql, params + [limit, offset]).fetchall()
            count_sql = (
                "SELECT COUNT(*) FROM (SELECT 1 FROM journals j "
                f"WHERE {where_clause} GROUP BY COALESCE(j.counterparty,'')"
                ") t"
            )
            total_groups = conn.execute(count_sql, params).fetchone()[0]
            groups = [
                {
                    "counterparty": r[0],
                    "journal_count": r[1],
                    "total_amount": r[2] or 0,
                }
                for r in rows
            ]
        finally:
            conn.close()

        template = env.get_template("summary_counterparty.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            groups=groups,
            total_groups=total_groups,
            q=q or "",
            source=source or "",
            date_from=date_from or "",
            date_to=date_to or "",
            limit=limit,
            offset=offset,
            order=order or "amount",
            dir=dir or "desc",
        )

    @app.get("/summary/counterparty/detail", response_class=HTMLResponse)
    def summary_counterparty_detail(
        request: Request,
        name: str,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> str:
        return RedirectResponse(
            url=_build_journals_url(
                query=f'cp:"{name}"',
                source=source,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                offset=offset,
            ),
            status_code=302,
        )

    # ========== Fixed Assets ==========
    @app.get("/fixed-assets", response_class=HTMLResponse)
    def fixed_assets_index(request: Request) -> str:
        conn = get_connection(app.state.db_path)
        try:
            rows = conn.execute(
                "SELECT id, name, acquisition_date, acquisition_cost, useful_life, method, business_use_ratio, accumulated_depreciation, fiscal_year, memo "
                "FROM fixed_assets WHERE fiscal_year = ? ORDER BY acquisition_date, id",
                (app.state.fiscal_year,),
            ).fetchall()
            items: list[dict[str, Any]] = []
            for r in rows:
                acq_cost = int(r[3])
                useful = int(r[4]) if r[4] else 1
                method = r[5] or "straight_line"
                ratio = int(r[6]) if r[6] is not None else 100
                accum = int(r[7]) if r[7] is not None else 0
                book = max(acq_cost - accum, 0)
                # very rough annual depreciation estimation for display
                if method == "declining_balance" and useful > 0:
                    rate = 2.0 / useful
                    est = int(book * rate)
                else:
                    est = int(acq_cost / max(useful, 1))
                est = int(est * (ratio / 100.0))
                est = max(min(est, book), 0)
                items.append(
                    {
                        "id": r[0],
                        "name": r[1],
                        "acquisition_date": r[2],
                        "acquisition_cost": acq_cost,
                        "useful_life": useful,
                        "method": method,
                        "business_use_ratio": ratio,
                        "accumulated_depreciation": accum,
                        "book_value": book,
                        "estimate_current_year": est,
                        "memo": r[9] or "",
                    }
                )
        finally:
            conn.close()
        template = env.get_template("fixed_assets.html")
        return template.render(request=request, fiscal_year=app.state.fiscal_year, assets=items)
    # CSV exports
    @app.get("/journals.csv")
    def journals_csv(
        request: Request,
        query: str | None = None,
        q: str | None = None,
        source: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        account_code: str | None = None,
        counterparty: str | None = None,
        source_file: str | None = None,
        amount_min: str | None = None,
        amount_max: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> StreamingResponse:
        counterparty = _blank_to_none(counterparty)
        source_file = _blank_to_none(source_file)
        source = _blank_to_none(source)
        date_from = _blank_to_none(date_from)
        date_to = _blank_to_none(date_to)
        account_code = _blank_to_none(account_code)
        parsed_query = _parse_journal_query(query)
        amount_min_value = _blank_to_int(parsed_query["amount_min"]) if query else _blank_to_int(amount_min)
        amount_max_value = _blank_to_int(parsed_query["amount_max"]) if query else _blank_to_int(amount_max)
        effective_q = parsed_query["q"] if query else q
        effective_source = parsed_query["source"] or source if query else source
        effective_date_from = parsed_query["date_from"] or date_from if query else date_from
        effective_date_to = parsed_query["date_to"] or date_to if query else date_to
        effective_account_code = parsed_query["account_code"] or account_code if query else account_code
        effective_category = parsed_query["category"] if query else None
        effective_counterparty = counterparty
        effective_source_file = parsed_query["source_file"] or source_file if query else source_file

        params = JournalSearchParams(
            fiscal_year=request.app.state.fiscal_year,
            date_from=effective_date_from,
            date_to=effective_date_to,
            account_code=effective_account_code,
            category=effective_category,
            description_contains=effective_q if not query else None,
            counterparty_contains=effective_counterparty,
            amount_min=amount_min_value,
            amount_max=amount_max_value,
            source=effective_source,
            source_file=effective_source_file,
            limit=(5000 if query else limit),
            offset=(0 if query else offset),
        )
        res = ledger_search(db_path=request.app.state.db_path, params=params)
        if query and isinstance(res, dict) and isinstance(res.get("journals"), list):
            names: dict[str, str] = {}
            conn = get_connection(request.app.state.db_path)
            try:
                for code, name in conn.execute("SELECT code, name FROM accounts").fetchall():
                    names[str(code)] = name
            finally:
                conn.close()
            res["journals"] = _filter_journals_by_query(
                res["journals"],
                free_terms=parsed_query["free_terms"],
                exclude_terms=parsed_query["exclude_terms"],
                description_terms=parsed_query["description_terms"],
                counterparty_terms=parsed_query["counterparty_terms"],
                account_names=names,
            )
            res["total_count"] = len(res["journals"])
        csv_text = _journals_to_csv(res)
        return StreamingResponse(_io.StringIO(csv_text), media_type="text/csv")

    @app.get("/gl/{account_code}.csv")
    def gl_csv(request: Request, account_code: str) -> StreamingResponse:
        res = ledger_general_ledger(
            db_path=request.app.state.db_path,
            fiscal_year=request.app.state.fiscal_year,
            account_code=account_code,
        )
        out = _io.StringIO()
        w = _csv.writer(out)
        w.writerow([
            "journal_id",
            "date",
            "description",
            "counter_account_code",
            "counter_account_name",
            "debit",
            "credit",
            "balance",
        ])
        for e in res.get("entries", []):
            w.writerow(
                [
                    e.get("journal_id"),
                    e.get("date"),
                    e.get("description", ""),
                    e.get("counter_account_code"),
                    e.get("counter_account_name"),
                    e.get("debit"),
                    e.get("credit"),
                    e.get("balance"),
                ]
            )
        return StreamingResponse(_io.StringIO(out.getvalue()), media_type="text/csv")

    @app.get("/trial-balance.csv")
    def tb_csv(request: Request) -> StreamingResponse:
        res = ledger_trial_balance(
            db_path=request.app.state.db_path, fiscal_year=request.app.state.fiscal_year
        )
        out = _io.StringIO()
        w = _csv.writer(out)
        w.writerow(["account_code", "account_name", "category", "debit_total", "credit_total", "balance"])
        for a in res.get("accounts", []):
            w.writerow(
                [
                    a.get("account_code"),
                    a.get("account_name"),
                    a.get("category"),
                    a.get("debit_total"),
                    a.get("credit_total"),
                    a.get("balance"),
                ]
            )
        return StreamingResponse(_io.StringIO(out.getvalue()), media_type="text/csv")

    # Import
    @app.get("/import", response_class=HTMLResponse)
    def import_index(request: Request) -> str:
        return RedirectResponse(url="/journals", status_code=307)
        template = env.get_template("import_upload.html")
        return template.render(request=request, fiscal_year=app.state.fiscal_year)

    @app.post("/import/upload", response_class=HTMLResponse)
    async def import_upload(request: Request, files: list[UploadFile] = File(...)) -> str:
        return PlainTextResponse("import UI is disabled", status_code=410)
        uploads = Path(__file__).parent / "../../../../shinkoku/shinkoku/work/uploads"
        uploads = uploads.resolve()
        uploads.mkdir(parents=True, exist_ok=True)
        saved_paths: list[str] = []
        results: list[dict] = []
        for f in files:
            target = uploads / f.filename
            content = await f.read()
            target.write_bytes(content)
            saved_paths.append(str(target))
            results.append(import_csv(file_path=str(target)))
        names = _account_names_map(app.state.db_path)
        template = env.get_template("import_preview.html")
        # Build combined count
        total_rows = sum(r.get("total_rows", 0) for r in results)
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            saved_paths=saved_paths,
            multi_results=results,
            total_rows=total_rows,
            account_names=names,
            default_credit="2030",
            default_source="csv_import",
        )

    @app.post("/import/preview", response_class=HTMLResponse)
    async def import_preview(
        request: Request,
        saved_paths: list[str] = Form(...),
        credit_code: str = Form("2030"),
        source: str = Form("csv_import"),
    ) -> str:
        return PlainTextResponse("import UI is disabled", status_code=410)
        names = _account_names_map(app.state.db_path)
        multi_results: list[dict] = []
        preview_entries: list[dict[str, Any]] = []
        total_rows = 0
        for sp in saved_paths:
            res = import_csv(file_path=sp)
            multi_results.append(res)
            total_rows += int(res.get("total_rows", 0))
            for c in res.get("candidates", []):
                debit = _suggest_debit_account(c.get("description", ""))
                amount = int(c.get("amount"))
                preview_entries.append(
                    {
                        "date": c.get("date"),
                        "description": c.get("description"),
                        "lines": [
                            {"side": "debit", "account_code": debit, "amount": amount},
                            {"side": "credit", "account_code": credit_code, "amount": amount},
                        ],
                        "source": source,
                        "source_file": sp,
                    }
                )
        template = env.get_template("import_preview.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            saved_paths=saved_paths,
            multi_results=multi_results,
            total_rows=total_rows,
            preview_entries=preview_entries,
            account_names=names,
            default_credit=credit_code,
            default_source=source,
        )

    @app.post("/import/register", response_class=HTMLResponse)
    async def import_register(
        request: Request,
        saved_paths: list[str] = Form(...),
        credit_code: str = Form("2030"),
        source: str = Form("csv_import"),
    ) -> str:
        return PlainTextResponse("import UI is disabled", status_code=410)
        entries: list[dict[str, Any]] = []
        for sp in saved_paths:
            res = import_csv(file_path=sp)
            for c in res.get("candidates", []):
                debit = _suggest_debit_account(c.get("description", ""))
                amount = int(c.get("amount"))
                entries.append(
                    {
                        "date": c.get("date"),
                        "description": c.get("description"),
                        "lines": [
                            {"side": "debit", "account_code": debit, "amount": amount},
                            {"side": "credit", "account_code": credit_code, "amount": amount},
                        ],
                        "source": source,
                        "source_file": sp,
                    }
                )
        py_entries = [JournalEntry(**e) for e in entries]
        result = ledger_add_journals_batch(
            db_path=app.state.db_path,
            fiscal_year=app.state.fiscal_year,
            entries=py_entries,
            force=True,
        )
        template = env.get_template("import_result.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            register_result=result,
            saved_path=", ".join(saved_paths),
        )

    # Audit log
    @app.get("/audit", response_class=HTMLResponse)
    def audit_index(request: Request, journal_id: int | None = None) -> str:
        res = ledger_audit_log(
            db_path=app.state.db_path,
            journal_id=journal_id,
            fiscal_year=app.state.fiscal_year,
        )
        template = env.get_template("audit.html")
        return template.render(request=request, fiscal_year=app.state.fiscal_year, audit=res)

    @app.get("/audit/detail", response_class=HTMLResponse)
    def audit_detail(request: Request, log_id: int) -> str:
        conn = get_connection(app.state.db_path)
        try:
            row = conn.execute(
                "SELECT id, journal_id, fiscal_year, operation, "
                "before_date, before_description, before_counterparty, before_lines_json, "
                "after_date, after_description, after_counterparty, after_lines_json, "
                "created_at FROM journal_audit_log WHERE id = ?",
                (log_id,),
            ).fetchone()
            if row is None:
                from fastapi.responses import PlainTextResponse
                return PlainTextResponse(f"audit log {log_id} not found", status_code=404)
            record = {
                "id": row[0],
                "journal_id": row[1],
                "fiscal_year": row[2],
                "operation": row[3],
                "before_date": row[4],
                "before_description": row[5],
                "before_counterparty": row[6],
                "before_lines_json": row[7] or "",
                "after_date": row[8],
                "after_description": row[9],
                "after_counterparty": row[10],
                "after_lines_json": row[11] or "",
                "created_at": row[12],
            }
            # Prettify JSON fields if possible
            def _pretty(s: str) -> str:
                try:
                    return _json.dumps(_json.loads(s), ensure_ascii=False, indent=2)
                except Exception:
                    return s or ""
            record["before_lines_pretty"] = _pretty(record["before_lines_json"]) if record["before_lines_json"] else ""
            record["after_lines_pretty"] = _pretty(record["after_lines_json"]) if record["after_lines_json"] else ""
            # Parse lines for diff view
            def _loads(s: str) -> list[dict]:
                try:
                    v = _json.loads(s) if s else []
                    return v if isinstance(v, list) else []
                except Exception:
                    return []
            before_lines = _loads(record["before_lines_json"])
            after_lines = _loads(record["after_lines_json"])
        finally:
            conn.close()

        template = env.get_template("audit_detail.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            rec=record,
            before_lines=before_lines,
            after_lines=after_lines,
        )

    # Path parameter variant for convenience
    @app.get("/audit/detail/{log_id}", response_class=HTMLResponse)
    def audit_detail_path(request: Request, log_id: int) -> str:
        return audit_detail(request, log_id)

    # Diff view for audit log (before/after highlighting)
    @app.get("/audit/diff", response_class=HTMLResponse)
    def audit_diff(request: Request, log_id: int) -> str:
        conn = get_connection(app.state.db_path)
        try:
            row = conn.execute(
                "SELECT id, journal_id, fiscal_year, operation, "
                "before_date, before_description, before_counterparty, before_lines_json, "
                "after_date, after_description, after_counterparty, after_lines_json, "
                "created_at FROM journal_audit_log WHERE id = ?",
                (log_id,),
            ).fetchone()
            if row is None:
                from fastapi.responses import PlainTextResponse
                return PlainTextResponse(f"audit log {log_id} not found", status_code=404)
            rec = {
                "id": row[0],
                "journal_id": row[1],
                "fiscal_year": row[2],
                "operation": row[3],
                "before_date": row[4],
                "before_description": row[5],
                "before_counterparty": row[6],
                "before_lines_json": row[7] or "",
                "after_date": row[8],
                "after_description": row[9],
                "after_counterparty": row[10],
                "after_lines_json": row[11] or "",
                "created_at": row[12],
            }
            def _loads(s: str) -> list[dict]:
                try:
                    v = _json.loads(s) if s else []
                    return v if isinstance(v, list) else []
                except Exception:
                    return []
            before_lines = _loads(rec["before_lines_json"])
            after_lines = _loads(rec["after_lines_json"])
        finally:
            conn.close()

        template = env.get_template("audit_diff.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            rec=rec,
            before_lines=before_lines,
            after_lines=after_lines,
        )

    @app.get("/audit/diff/{log_id}", response_class=HTMLResponse)
    def audit_diff_path(request: Request, log_id: int) -> str:
        return audit_diff(request, log_id)

    # ===== AI Reading (Receipt OCR via LM Studio Vision) =====
    @app.get("/ai/reading-receipt", response_class=HTMLResponse)
    def ai_reading_receipt_index(request: Request) -> str:
        return RedirectResponse(url="/journals", status_code=307)
        template = env.get_template("ai_reading_upload.html")
        cfg = _llm_config()
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            api_url=cfg["api_url"],
            model=cfg["model"],
        )

    @app.post("/ai/reading-receipt", response_class=HTMLResponse)
    async def ai_reading_receipt_upload(
        request: Request,
        files: list[UploadFile] = File(...),
        note: str | None = None,
    ) -> str:
        return PlainTextResponse("AI reading UI is disabled", status_code=410)
        # Save files under work/uploads
        uploads = Path(__file__).parent / "../../../../shinkoku/shinkoku/work/uploads"
        uploads = uploads.resolve()
        uploads.mkdir(parents=True, exist_ok=True)
        names = _account_names_map(app.state.db_path)

        file_results: list[dict] = []
        previews: list[dict | None] = []
        saved_paths: list[str] = []

        for f in files:
            target = uploads / f.filename
            content = await f.read()
            if len(content) > 16 * 1024 * 1024:
                return PlainTextResponse("画像が大きすぎます（16MB超）", status_code=400)
            target.write_bytes(content)
            saved_paths.append(str(target))
            # Call LM Studio
            try:
                if (f.content_type or "").lower() == "application/pdf" or str(target).lower().endswith(".pdf"):
                    png = _pdf_first_page_to_png(content)
                    result = _llm_extract_receipt(png, note=note)
                else:
                    result = _llm_extract_receipt(content, note=note)
            except Exception as e:
                return PlainTextResponse(f"LLM呼び出しエラー: {e}", status_code=500)
            file_results.append(result)
            # Build preview
            try:
                total = int(result.get("total_amount", 0))
                desc = result.get("vendor") or "レシート"
                debit = _suggest_debit_account(desc)
                previews.append(
                    {
                        "date": result.get("date"),
                        "description": desc,
                        "lines": [
                            {"side": "debit", "account_code": debit, "amount": total},
                            {"side": "credit", "account_code": "1001", "amount": total},
                        ],
                        "source": "receipt_ocr",
                        "source_file": str(target),
                    }
                )
            except Exception:
                previews.append(None)

        template = env.get_template("ai_reading_preview.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            saved_paths=saved_paths,
            ocr_multi=file_results,
            preview_multi=previews,
            account_names=names,
            default_credit="1001",
        )

    @app.post("/ai/reading-receipt/register", response_class=HTMLResponse)
    async def ai_reading_receipt_register(
        request: Request,
        saved_path: list[str] = Form(...),
        date: list[str] = Form(...),
        description: list[str] = Form(...),
        debit_code: list[str] = Form(...),
        credit_code: list[str] = Form(...),
        amount: list[int] = Form(...),
    ) -> str:
        return PlainTextResponse("AI reading UI is disabled", status_code=410)
        entries: list[JournalEntry] = []
        n = len(saved_path)
        for i in range(n):
            entries.append(
                JournalEntry(
                    date=date[i],
                    description=description[i],
                    lines=[
                        {"side": "debit", "account_code": debit_code[i], "amount": int(amount[i])},
                        {"side": "credit", "account_code": credit_code[i], "amount": int(amount[i])},
                    ],
                    source="receipt_ocr",
                    source_file=saved_path[i],
                )
            )
        res = ledger_add_journals_batch(
            db_path=app.state.db_path,
            fiscal_year=app.state.fiscal_year,
            entries=entries,
            force=True,
        )
        template = env.get_template("import_result.html")
        return template.render(
            request=request,
            fiscal_year=app.state.fiscal_year,
            register_result=res,
            saved_path=", ".join(saved_path),
        )

    return app

def _account_names_map(db_path: str) -> dict[str, str]:
    names: dict[str, str] = {}
    conn = get_connection(db_path)
    try:
        for code, name in conn.execute("SELECT code, name FROM accounts").fetchall():
            names[str(code)] = name
    finally:
        conn.close()
    return names


def _suggest_debit_account(description: str) -> str:
    s = (description or "")
    upper = s.upper()
    # Simple heuristics (do not modify core logic)
    if any(k in s for k in ["オートチャージ", "駅", "JR", "チャージ", "PASMO", "Suica", "SUICA"]):
        return "5130"  # 旅費交通費
    if any(k in upper for k in ["KDDI", "AU", "DOCOMO", "SOFTBANK", "UQ", "IIJ", "MINEO"]):
        return "5140"  # 通信費
    return "5270"  # 雑費 (default)


def _journals_to_csv(journals: dict) -> str:
    out = _io.StringIO()
    w = _csv.writer(out)
    w.writerow(["journal_id", "date", "description", "counterparty", "side", "account_code", "amount", "tax_category", "tax_amount"])
    for j in journals.get("journals", []):
        for li in j.get("lines", []):
            w.writerow([
                j.get("id"), j.get("date"), j.get("description", ""), j.get("counterparty", ""),
                li.get("side"), li.get("account_code"), li.get("amount"), li.get("tax_category", ""), li.get("tax_amount", 0)
            ])
    return out.getvalue()


def _llm_config() -> dict[str, str]:
    api_url = os.environ.get("LMSTUDIO_API_URL", "http://192.168.40.182:1234/v1")
    model = os.environ.get("LMSTUDIO_MODEL", "gemma-4-e4b-it")
    api_key = os.environ.get("LMSTUDIO_API_KEY", "")
    return {"api_url": api_url.rstrip("/"), "model": model, "api_key": api_key}


def _openai_vision_payload(model: str, image_bytes: bytes, note: Optional[str] = None) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    user_text = (
        "次のレシート画像から厳密なJSONを抽出してください。"
        "出力は日本語テキストを含まず、以下のキーのみを持つJSONオブジェクト1つです。"
        "{date: 'YYYY-MM-DD', vendor: string, total_amount: int, tax_included: boolean, items: ["
        "{name: string, qty?: number, unit_price?: int, amount: int}]}."
    )
    if note:
        user_text += f" ヒント: {note}"
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 800,
        "messages": [
            {
                "role": "system",
                "content": "あなたは日本のレシートを構造化する抽出器です。JSONのみを返します。",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            },
        ],
        # LM Studio 実装では json_schema/text のみを受け付ける場合があるため text を指定
        "response_format": {"type": "text"},
    }
    return payload


def _llm_extract_receipt(image_bytes: bytes, *, note: Optional[str] = None) -> dict:
    if requests is None:
        raise RuntimeError("requests が未インストールです。pip install requests")
    cfg = _llm_config()
    url = f"{cfg['api_url']}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    payload = _openai_vision_payload(cfg["model"], image_bytes, note)

    # Call with response_format=text
    resp = requests.post(url, headers=headers, data=_json.dumps(payload), timeout=45)
    if resp.status_code >= 400:
        # Fallback: remove response_format and retry once
        try:
            p2 = dict(payload)
            p2.pop("response_format", None)
            resp = requests.post(url, headers=headers, data=_json.dumps(p2), timeout=45)
        except Exception:
            raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    text = _extract_choice_text(data)
    obj = _coerce_json(text)
    return obj


def _pdf_first_page_to_png(pdf_bytes: bytes) -> bytes:
    try:
        import pypdfium2 as pdfium  # type: ignore
        from PIL import Image  # noqa: F401
        import io
        pdf = pdfium.PdfDocument(io.BytesIO(pdf_bytes))
        if len(pdf) == 0:
            raise RuntimeError("PDFにページがありません")
        page = pdf[0]
        bitmap = page.render(scale=2)
        pil = bitmap.to_pil()
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        raise RuntimeError(f"PDFレンダリング失敗: {e}")


def _extract_choice_text(data: dict) -> str:
    try:
        return data["choices"][0]["message"]["content"]
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"LLM応答の解析に失敗: {e}; data={str(data)[:200]}")


def _coerce_json(text: str) -> dict:
    # Remove code fences if present
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`\n ")
        # remove possible leading 'json' identifier
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        return _json.loads(s)
    except Exception as e:
        raise RuntimeError(f"JSONデコード失敗: {e}; text={s[:200]}")
