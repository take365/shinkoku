from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin

import yaml
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DEFAULT_OUTPUT_DIR = ROOT_DIR / "output" / "web-ui-latest"
DEFAULT_CONFIG_PATH = ROOT_DIR / "shinkoku.config.yaml"
DEFAULT_SAMPLES_DIR = ROOT_DIR / "input_samples"
DEFAULT_REFERENCE_EXPORT = ROOT_DIR / "output" / "exports" / "journals_2025.csv"
DEFAULT_APP_PORT = 8010
DEFAULT_DEMO_PORT = 8011


def _load_config_defaults(config_path: Path) -> tuple[str | None, int | None]:
    if not config_path.exists():
        return None, None
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    db_path = data.get("db_path")
    tax_year = data.get("tax_year")
    resolved_db_path = None
    if db_path:
        raw = Path(str(db_path))
        resolved_db_path = str((config_path.parent / raw).resolve() if not raw.is_absolute() else raw)
    return resolved_db_path, int(tax_year) if tax_year is not None else None


def _is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _wait_for_server(base_url: str, timeout_sec: float = 15.0) -> None:
    import urllib.request

    deadline = time.time() + timeout_sec
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1.0) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # pragma: no cover - best effort polling
            last_error = exc
            time.sleep(0.3)
    raise RuntimeError(f"Web UI did not become ready: {last_error}")


def _start_server(*, db_path: str, fiscal_year: int, host: str, port: int, log_dir: Path) -> subprocess.Popen[str]:
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout = (log_dir / "capture-server.out.log").open("w", encoding="utf-8")
    stderr = (log_dir / "capture-server.err.log").open("w", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT_DIR / 'src'}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    return subprocess.Popen(
        [
            str(ROOT_DIR / ".venv" / "bin" / "python"),
            "-m",
            "shinkoku.cli",
            "web",
            "--db-path",
            db_path,
            "--fiscal-year",
            str(fiscal_year),
            "--host",
            host,
            "--port",
            str(port),
        ],
        cwd=str(ROOT_DIR),
        env=env,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )


def _safe_wait(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=3_000)
    except PlaywrightTimeoutError:
        page.wait_for_timeout(500)


def _capture_page(
    page,
    *,
    base_url: str,
    output_dir: Path,
    order: int,
    label: str,
    path: str,
) -> dict[str, str]:
    url = f"{base_url}{path}"
    page.goto(url, wait_until="domcontentloaded")
    _safe_wait(page)
    target = output_dir / f"{order:02d}_{label}.png"
    page.screenshot(path=str(target), full_page=True)
    return {"order": f"{order:02d}", "label": label, "url": url, "file": target.name, "kind": "screenshot"}


def _download_file(*, base_url: str, output_dir: Path, order: int, label: str, path: str) -> dict[str, str]:
    import urllib.request

    url = f"{base_url}{path}"
    target = output_dir / f"{order:02d}_{label}.csv"
    with urllib.request.urlopen(url, timeout=10) as response:
        target.write_bytes(response.read())
    return {"order": f"{order:02d}", "label": label, "url": url, "file": target.name, "kind": "download"}


def _first_href(page, selector: str, *, exclude_substrings: list[str] | None = None) -> str | None:
    exclude_substrings = exclude_substrings or []
    locator = page.locator(selector)
    count = locator.count()
    for idx in range(count):
        href = locator.nth(idx).get_attribute("href")
        if not href:
            continue
        if any(token in href for token in exclude_substrings):
            continue
        return href
    return None


def _build_routes() -> list[tuple[str, str]]:
    return [
        ("仕訳一覧", "/journals"),
        ("入力元を表示", "/journals?show_source=1"),
        ("勘定科目サマリ", "/trial-balance"),
        ("摘要サマリ", "/summary/description"),
        ("取引先サマリ", "/summary/counterparty"),
        ("月次サマリ", f"/summary/monthly?{urlencode({'date_from': '2025-01-01', 'date_to': '2025-12-31', 'category': 'expense'})}"),
        ("入力元サマリ", "/source-links"),
        ("PL", "/pl"),
        ("BS", "/bs"),
        ("重複候補", "/duplicates?threshold=70"),
        ("固定資産", "/fixed-assets"),
        ("監査ログ", "/audit"),
    ]


def _resolve_base_url(args: argparse.Namespace) -> tuple[int, str]:
    if args.base_url:
        return args.port, args.base_url
    if args.build_demo_db and args.port == DEFAULT_APP_PORT:
        args.port = DEFAULT_DEMO_PORT
    return args.port, f"http://{args.host}:{args.port}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture latest Web UI screenshots into one directory.")
    parser.add_argument("--db-path")
    parser.add_argument("--fiscal-year", type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=DEFAULT_APP_PORT, type=int)
    parser.add_argument("--base-url")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--browser-path", default="")
    parser.add_argument("--samples-dir", default=str(DEFAULT_SAMPLES_DIR))
    parser.add_argument("--reference-export", default=str(DEFAULT_REFERENCE_EXPORT))
    parser.add_argument("--build-demo-db", action="store_true")
    args = parser.parse_args()

    config_db_path, config_fiscal_year = _load_config_defaults(Path(args.config))
    db_path = args.db_path or config_db_path
    fiscal_year = args.fiscal_year or config_fiscal_year
    if not db_path or not fiscal_year:
        raise SystemExit("--db-path and --fiscal-year are required when config defaults are unavailable")

    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.build_demo_db:
        from tests.helpers.demo_data import build_capture_demo_db

        db_path = str(output_dir / "capture_demo.db")
        build_capture_demo_db(
            db_path=db_path,
            fiscal_year=fiscal_year,
            samples_dir=args.samples_dir,
            reference_export_path=args.reference_export,
        )

    port, base_url = _resolve_base_url(args)
    started_server = False
    server_process: subprocess.Popen[str] | None = None

    try:
        if args.build_demo_db:
            if _is_port_open(args.host, port):
                raise SystemExit(
                    f"Demo capture port {port} is already in use. "
                    "Stop the existing server or pass --port with an unused port."
                )
            started_server = True
            server_process = _start_server(
                db_path=db_path,
                fiscal_year=fiscal_year,
                host=args.host,
                port=port,
                log_dir=output_dir,
            )
            _wait_for_server(base_url)
        elif not _is_port_open(args.host, port):
            started_server = True
            server_process = _start_server(
                db_path=db_path,
                fiscal_year=fiscal_year,
                host=args.host,
                port=port,
                log_dir=output_dir,
            )
            _wait_for_server(base_url)

        with sync_playwright() as playwright:
            launch_kwargs: dict[str, object] = {"headless": True}
            browser_path = args.browser_path or shutil.which("chromium-browser") or shutil.which("chromium")
            if browser_path:
                launch_kwargs["executable_path"] = browser_path
            browser = playwright.chromium.launch(**launch_kwargs)
            page = browser.new_page(viewport={"width": 1440, "height": 2200}, device_scale_factor=1)

            manifest: list[dict[str, str]] = []

            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=1, label="仕訳一覧", path="/journals"))
            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=2, label="入力元を表示", path="/journals?show_source=1"))
            manifest.append(_download_file(base_url=base_url, output_dir=output_dir, order=3, label="ダウンロード（csv）", path="/journals.csv"))
            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=4, label="勘定科目サマリ", path="/trial-balance"))
            page.goto(f"{base_url}/trial-balance", wait_until="domcontentloaded")
            _safe_wait(page)
            journals_acc_href = _first_href(page, 'tbody a[href^="/journals?query=acc%3A"]')
            if journals_acc_href:
                manifest.append(
                    _capture_page(
                        page,
                        base_url=base_url,
                        output_dir=output_dir,
                        order=5,
                        label="仕訳一覧（acc）",
                        path=journals_acc_href,
                    )
                )
            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=6, label="総勘定元帳（普通預金）", path="/gl/1002"))

            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=7, label="摘要サマリ", path="/summary/description"))
            page.goto(f"{base_url}/summary/description", wait_until="domcontentloaded")
            _safe_wait(page)
            summary_description_href = _first_href(page, 'tbody a[href^="/journals?query="]')
            if summary_description_href:
                manifest.append(
                    _capture_page(
                        page,
                        base_url=base_url,
                        output_dir=output_dir,
                        order=8,
                        label="仕訳一覧（desc）",
                        path=summary_description_href,
                    )
                )

            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=9, label="取引先サマリ", path="/summary/counterparty"))
            page.goto(f"{base_url}/summary/counterparty", wait_until="domcontentloaded")
            _safe_wait(page)
            summary_counterparty_href = _first_href(
                page,
                'tbody a[href^="/journals?query="]',
                exclude_substrings=['cp%3A%22%22', 'cp:""'],
            )
            if summary_counterparty_href:
                manifest.append(
                    _capture_page(
                        page,
                        base_url=base_url,
                        output_dir=output_dir,
                        order=10,
                        label="仕訳一覧（cp）",
                        path=summary_counterparty_href,
                    )
                )

            manifest.append(
                _capture_page(
                    page,
                    base_url=base_url,
                    output_dir=output_dir,
                    order=11,
                    label="月次サマリ",
                    path=f"/summary/monthly?{urlencode({'date_from': '2025-01-01', 'date_to': '2025-12-31', 'category': 'expense'})}",
                )
            )
            page.goto(f"{base_url}/summary/monthly", wait_until="domcontentloaded")
            _safe_wait(page)
            summary_monthly_href = _first_href(page, 'tbody td:nth-child(2) a[href*="cat%3A"]')
            if not summary_monthly_href:
                summary_monthly_href = _first_href(page, 'tbody a[href*="cat%3A"]')
            if summary_monthly_href:
                manifest.append(
                    _capture_page(
                        page,
                        base_url=base_url,
                        output_dir=output_dir,
                        order=12,
                        label="仕訳一覧（from_to_cat）",
                        path=summary_monthly_href,
                    )
                )

            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=13, label="入力元サマリ", path="/source-links"))
            page.goto(f"{base_url}/source-links", wait_until="domcontentloaded")
            _safe_wait(page)
            source_detail_href = _first_href(page, 'a[href^="/source-links/detail"]')
            if source_detail_href:
                manifest.append(
                    _capture_page(
                        page,
                        base_url=base_url,
                        output_dir=output_dir,
                        order=14,
                        label="入力元詳細",
                        path=source_detail_href,
                    )
                )
                page.goto(urljoin(base_url, source_detail_href), wait_until="domcontentloaded")
                _safe_wait(page)
                source_file_journals_href = _first_href(page, 'a[href^="/journals?query=file%3A"]')
                if source_file_journals_href:
                    manifest.append(
                        _capture_page(
                            page,
                            base_url=base_url,
                            output_dir=output_dir,
                            order=15,
                            label="仕訳一覧（file）",
                            path=source_file_journals_href,
                        )
                    )

            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=16, label="PL", path="/pl"))
            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=17, label="BS", path="/bs"))
            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=18, label="重複候補", path="/duplicates?threshold=70"))
            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=20, label="固定資産", path="/fixed-assets"))
            manifest.append(_capture_page(page, base_url=base_url, output_dir=output_dir, order=21, label="監査ログ", path="/audit"))

            page.goto(f"{base_url}/audit", wait_until="domcontentloaded")
            _safe_wait(page)
            audit_detail_href = _first_href(page, 'a[href^="/audit/detail"]')
            if audit_detail_href:
                manifest.append(
                    _capture_page(
                        page,
                        base_url=base_url,
                        output_dir=output_dir,
                        order=22,
                        label="監査ログ詳細",
                        path=audit_detail_href,
                    )
                )

            page.goto(f"{base_url}/duplicates?threshold=70", wait_until="domcontentloaded")
            _safe_wait(page)
            duplicate_journals_href = _first_href(page, 'a[href^="/journals?query=from%3A"]')
            if duplicate_journals_href:
                manifest.append(
                    _capture_page(
                        page,
                        base_url=base_url,
                        output_dir=output_dir,
                        order=19,
                        label="仕訳一覧（from_to_mfrom_mto）",
                        path=duplicate_journals_href,
                    )
                )

            browser.close()

        (output_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "base_url": base_url,
                    "db_path": db_path,
                    "fiscal_year": fiscal_year,
                    "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "files": sorted(manifest, key=lambda item: int(item["order"])),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(str(output_dir))
        return 0
    finally:
        if started_server and server_process is not None:
            server_process.terminate()
            try:
                server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server_process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
