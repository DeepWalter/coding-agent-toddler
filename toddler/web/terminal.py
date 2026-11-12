"""``/ws/terminal`` — a PTY-backed shell for the web editor.

One shell per connection.  Terminal output is forwarded as binary frames —
never decoded on the server, because a UTF-8 character can straddle two
reads — while input and resize requests arrive as JSON.  POSIX only: on
other platforms the endpoint answers an error and closes.

The endpoint is an ungated shell, so it admits only loopback clients and
only when the handshake's ``Origin`` matches the served app (or, in dev,
the Vite server).  ``tod serve --host 0.0.0.0`` disables it unless
``--allow-terminal`` says otherwise.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import ipaddress
import os
import pty
import shutil
import struct
import termios
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from toddler.web.state import WebAppState

__all__ = ["router"]

router = APIRouter()

#: Largest terminal accepted from the client, and the fallback size used
#: until its first resize arrives.
_MAX_COLS = 500
_MAX_ROWS = 300
_DEFAULT_COLS = 80
_DEFAULT_ROWS = 24

#: One PTY read per frame; a burst of output becomes a handful of sends.
_READ_SIZE = 65536

#: Grace between SIGTERM and SIGKILL when a session is torn down.
_TERMINATE_TIMEOUT = 2.0

#: Browser origins the Vite dev server may open a terminal from (its proxy
#: connects to the app from loopback, so the client check still passes).
_DEV_ORIGINS = {"http://localhost:5173", "http://127.0.0.1:5173"}


def _origin_allowed(websocket: WebSocket, dev: bool) -> bool:
    """Whether the handshake's ``Origin`` may open a shell.

    A browser always sends ``Origin``; matching it against ``Host`` is what
    stops another page in the same browser from driving the loopback server
    (cross-site WebSocket hijacking).  Native clients, curl, and the test
    client send none.
    """
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    host = websocket.headers.get("host")
    if not host:
        return False
    allowed = {f"http://{host}", f"https://{host}"}
    if dev:
        allowed |= _DEV_ORIGINS
    return origin in allowed


def _client_is_loopback(websocket: WebSocket) -> bool:
    """Whether the peer is on this machine.

    A client whose address is not an IP — the in-process test client, a
    Unix socket — cannot be reached over a network, so it passes; a real
    peer is checked by address.
    """
    client = websocket.client
    if client is None:
        return True
    try:
        address = ipaddress.ip_address(client.host)
    except ValueError:
        return True
    return address.is_loopback


def _choose_shell() -> str | None:
    """The user's login shell, or the first usable fallback."""
    shell = os.environ.get("SHELL", "")
    if os.path.isabs(shell) and os.access(shell, os.X_OK):
        return shell
    for candidate in ("/bin/zsh", "/bin/bash", "/bin/sh"):
        if os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("sh")


def _child_setup() -> None:
    """Make the inherited slave fd the child's controlling terminal.

    ``stdin``/``stdout``/``stderr`` already point at the slave when this
    runs.  Without a controlling tty, bash and zsh disable job control and
    Ctrl-C never reaches the foreground program.
    """
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    """Apply a terminal size (silently ignoring a PTY that already went away)."""
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _dimension(value: object, default: int, maximum: int) -> int:
    """Clamp a client-supplied row/column count to ``[1, maximum]``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(1, min(maximum, value))


async def _refuse(websocket: WebSocket, message: str) -> None:
    """Accept a handshake only to explain why it cannot have a shell.

    Closing before ``accept()`` is an HTTP 403 with no body, leaving the
    frontend to guess; accepting first lets the panel print the reason.
    """
    await websocket.accept()
    with contextlib.suppress(Exception):
        await websocket.send_json({"type": "error", "message": message})
    with contextlib.suppress(Exception):
        await websocket.close(code=1008)


async def _send_error_and_close(websocket: WebSocket, message: str) -> None:
    with contextlib.suppress(Exception):
        await websocket.send_json({"type": "error", "message": message})
    with contextlib.suppress(Exception):
        await websocket.close(code=1011)


def _kill_now(proc: asyncio.subprocess.Process | None) -> None:
    """SIGKILL the shell without waiting — the teardown-cancelled path."""
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def _terminate(proc: asyncio.subprocess.Process | None) -> None:
    """SIGTERM the shell, then SIGKILL it if it lingers."""
    if proc is None or proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), _TERMINATE_TIMEOUT)
    except TimeoutError:
        _kill_now(proc)
        with contextlib.suppress(Exception):
            await proc.wait()


async def _run_session(
    websocket: WebSocket, state: WebAppState, shell: str,
) -> None:
    """Spawn *shell* on a new PTY and shuttle bytes until either side ends."""
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError as exc:
        await _send_error_and_close(websocket, f"cannot open a pty: {exc}")
        return
    _set_winsize(master_fd, _DEFAULT_COLS, _DEFAULT_ROWS)

    env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            shell,
            "-l",
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(state.repo_root),
            env=env,
            preexec_fn=_child_setup,
        )
    except (OSError, ValueError) as exc:
        os.close(slave_fd)
        os.close(master_fd)
        await _send_error_and_close(websocket, f"cannot start {shell}: {exc}")
        return
    os.close(slave_fd)

    loop = asyncio.get_running_loop()
    output: asyncio.Queue[bytes | None] = asyncio.Queue()

    def on_readable() -> None:
        try:
            chunk = os.read(master_fd, _READ_SIZE)
        except OSError:
            chunk = b""
        if chunk:
            output.put_nowait(chunk)
        else:
            # EOF — stop reading, and hand the pump its sentinel so every
            # preceding chunk is flushed before the exit frame.
            with contextlib.suppress(Exception):
                loop.remove_reader(master_fd)
            output.put_nowait(None)

    loop.add_reader(master_fd, on_readable)

    async def pump() -> None:
        while True:
            chunk = await output.get()
            if chunk is None:
                break
            await websocket.send_bytes(chunk)
        code = await proc.wait()
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "exit", "code": code})
        with contextlib.suppress(Exception):
            await websocket.close()

    async def receive() -> None:
        try:
            async for raw in websocket.iter_json():
                if not isinstance(raw, dict):
                    continue
                kind = raw.get("type")
                if kind == "input":
                    data = raw.get("data")
                    if isinstance(data, str) and data:
                        with contextlib.suppress(OSError):
                            os.write(master_fd, data.encode("utf-8"))
                elif kind == "resize":
                    cols = _dimension(raw.get("cols"), _DEFAULT_COLS, _MAX_COLS)
                    rows = _dimension(raw.get("rows"), _DEFAULT_ROWS, _MAX_ROWS)
                    _set_winsize(master_fd, cols, rows)
        except (WebSocketDisconnect, RuntimeError):
            # The client went away (or the pump closed the socket after an
            # exit); the task's job is done either way.
            pass

    tasks: list[asyncio.Task[None]] = []
    try:
        # Sent inside the guarded block: a client that vanishes between the
        # spawn and this frame must still close the PTY and reap the shell.
        await websocket.send_json(
            {"type": "ready", "shell": shell, "cwd": str(state.repo_root)},
        )
        tasks = [asyncio.create_task(receive()), asyncio.create_task(pump())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        # The server or the test client is tearing the connection down.  Run
        # the cleanup below, then return normally: a cancelled endpoint ends
        # with a cancelled task, which makes a TestClient session's teardown
        # raise even though the shell was cleaned up.
        pass
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(master_fd)
        # Closing the master SIGHUPs the session; terminate below makes it
        # unconditional for a shell that ignored the hangup.
        with contextlib.suppress(OSError):
            os.close(master_fd)
        for task in tasks:
            task.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
            await _terminate(proc)
        except asyncio.CancelledError:
            # Cancellation during teardown — SIGTERM cannot be awaited, so
            # end the shell outright.  Nothing above is left to clean up.
            _kill_now(proc)


@router.websocket("/ws/terminal")
async def ws_terminal(websocket: WebSocket) -> None:
    """Open a terminal session, or explain why one is not available."""
    state: WebAppState = websocket.app.state.web
    if not state.terminal_enabled:
        await _refuse(websocket, "the terminal is disabled on this server.")
        return
    if not _origin_allowed(websocket, state.dev):
        await _refuse(websocket, "origin not allowed for the terminal.")
        return
    if not _client_is_loopback(websocket):
        await _refuse(websocket, "the terminal is only available on loopback.")
        return
    if os.name != "posix":
        await _refuse(websocket, "the terminal needs a POSIX platform.")
        return
    shell = _choose_shell()
    if shell is None:
        await _refuse(websocket, "no usable shell was found.")
        return

    await websocket.accept()
    await _run_session(websocket, state, shell)
