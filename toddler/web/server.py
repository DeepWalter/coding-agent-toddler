"""uvicorn bootstrap for ``tod serve``."""

from __future__ import annotations

import ipaddress
import threading
import webbrowser
from pathlib import Path

import uvicorn

from toddler.config.settings import Settings
from toddler.web.app import create_app

__all__ = ["run_server"]


def _is_loopback_host(host: str) -> bool:
    """Whether *host* names this machine.

    The terminal endpoint is an ungated shell; it is enabled by default
    only when the server binds a loopback address.
    """
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost",)


def run_server(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = True,
    dev: bool = False,
    new_session: bool = False,
    allow_terminal: bool = False,
) -> None:
    """Run the FastAPI app under uvicorn (blocks until interrupted).

    ``log_config=None`` keeps uvicorn from reconfiguring logging — the
    CLI's ``setup_logging`` file handler already routes INFO+ messages to
    ``~/.toddler/logs``.
    """
    app = create_app(
        settings,
        repo_root=Path.cwd(),
        dev=dev,
        new_session=new_session,
        terminal_enabled=allow_terminal or _is_loopback_host(host),
    )

    if open_browser:
        # A wildcard bind host isn't openable by the browser — point it
        # at localhost instead.
        browse_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        threading.Timer(
            1.5,
            lambda: webbrowser.open(f"http://{browse_host}:{port}"),
        ).start()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_config=None,
    )
