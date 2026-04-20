from __future__ import annotations

from argparse import Namespace

from tests.tools.capture_web_ui_screenshots import (
    DEFAULT_APP_PORT,
    DEFAULT_DEMO_PORT,
    _resolve_base_url,
)


def test_resolve_base_url_keeps_default_port_for_normal_capture() -> None:
    args = Namespace(
        base_url=None,
        build_demo_db=False,
        host="127.0.0.1",
        port=DEFAULT_APP_PORT,
    )

    port, base_url = _resolve_base_url(args)

    assert port == DEFAULT_APP_PORT
    assert base_url == "http://127.0.0.1:8010"


def test_resolve_base_url_uses_demo_port_for_demo_capture() -> None:
    args = Namespace(
        base_url=None,
        build_demo_db=True,
        host="127.0.0.1",
        port=DEFAULT_APP_PORT,
    )

    port, base_url = _resolve_base_url(args)

    assert port == DEFAULT_DEMO_PORT
    assert base_url == "http://127.0.0.1:8011"


def test_resolve_base_url_keeps_explicit_port_for_demo_capture() -> None:
    args = Namespace(
        base_url=None,
        build_demo_db=True,
        host="127.0.0.1",
        port=8123,
    )

    port, base_url = _resolve_base_url(args)

    assert port == 8123
    assert base_url == "http://127.0.0.1:8123"
