"""Shared helpers for the espbridge MCP server: error wrapping, live action
feedback, and byte/hex/info conversion."""
from __future__ import annotations

import functools

from .. import constants as C
from .._log import log
from ..errors import BridgeError

try:  # FastMCP surfaces ToolError messages to the client verbatim
    from fastmcp.exceptions import ToolError
except Exception:  # pragma: no cover - fallback if the layout changes
    class ToolError(Exception):
        pass

try:  # context access for client-facing feedback (fastmcp is always present here)
    import anyio
    from fastmcp.server.dependencies import get_context
except Exception:  # pragma: no cover
    anyio = None
    get_context = None

# Exceptions a sub-API raises for ordinary "bad call" situations — turned into
# clean ToolError messages instead of opaque server-side failures.
_USER_ERRORS = (BridgeError, ValueError, KeyError, OSError, TimeoutError)


class _Feedback:
    """Toggle for live board-action feedback. On by default; flip ``enabled``
    from the CLI (--no-feedback) or the ``bridge_feedback`` tool at runtime."""

    def __init__(self) -> None:
        self.enabled = True


FEEDBACK = _Feedback()


def _short(value, limit: int = 200) -> str:
    s = value if isinstance(value, str) else repr(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _feedback_message(name: str, params: dict, result) -> str:
    # When the tool already returns a human-readable confirmation string (e.g.
    # "GPIO2 set to output"), that string is the feedback. For structured
    # results (dicts, ints, etc.), format as "call(args) -> short_repr".
    if isinstance(result, str):
        return result
    args = ", ".join(f"{k}={v!r}" for k, v in params.items())
    head = f"{name}({args})"
    return head if result is None else f"{head} -> {_short(result)}"


def _notify_client(msg: str, level: str = "info") -> None:
    # FastMCP runs each tool in a synchronous worker thread, so we use
    # anyio.from_thread.run to hop back to the async event loop in order to
    # send the MCP notification. If that fails (client disconnected, no active
    # portal), we silently drop it — the server log still captures the message.
    try:
        ctx = get_context()
    except RuntimeError:
        return  # no active request (direct call / tests)
    send = ctx.warning if level == "warning" else ctx.info
    try:
        anyio.from_thread.run(send, msg)
    except Exception:
        pass


def emit_feedback(name: str, params: dict, result, *, ok: bool = True) -> None:
    """Surface a one-line 'what happened on the board' message — to the server
    log (operator) and, when a request is active, to the MCP client (agent and
    user). Reports failures too (ok=False), so every action is visible. Never
    raises: feedback must not break a tool."""
    try:
        if ok:
            msg, level = _feedback_message(name, params, result), "info"
            log.info(msg)
        else:
            args = ", ".join(f"{k}={v!r}" for k, v in params.items())
            msg = f"{name}({args}) FAILED: {type(result).__name__}: {result}"
            level = "warning"
            log.warning(msg)
        if get_context is not None:
            _notify_client(msg, level)
    except Exception:
        pass


def guarded(fn):
    """Wrap a tool body so peripheral errors come back as readable ToolErrors,
    and emit live feedback after every call — success or failure.

    ``functools.wraps`` preserves the signature/annotations FastMCP inspects to
    build the tool schema, so this composes under ``@mcp.tool`` transparently.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        # FastMCP calls tools with keyword arguments, so kwargs holds the
        # named parameters we pass to emit_feedback for display.
        try:
            result = fn(*args, **kwargs)
        except _USER_ERRORS as e:
            if FEEDBACK.enabled:
                emit_feedback(fn.__name__, kwargs, e, ok=False)
            raise ToolError(f"{type(e).__name__}: {e}") from e
        if FEEDBACK.enabled:
            emit_feedback(fn.__name__, kwargs, result)
        return result

    return wrapper


def to_bytes(value: str | None) -> bytes:
    """Parse a hex string into bytes. Accepts ``"01ff"``, ``"01 ff"``,
    ``"01:ff"`` or ``"01,ff"``; ``None``/empty -> ``b""``."""
    if not value:
        return b""
    s = value.strip()
    for sep in (":", ",", "-"):
        s = s.replace(sep, " ")
    if " " in s:
        return bytes(int(tok, 16) for tok in s.split())
    return bytes.fromhex(s)


def hex_str(data: bytes) -> str:
    """Bytes -> lowercase hex (the form ``to_bytes`` reads back)."""
    return data.hex()


def caps_list(caps: C.Cap) -> list[str]:
    return [c.name for c in C.Cap if c in caps]


def info_dict(esp) -> dict | None:
    """Serialize Bridge.info to a JSON-friendly dict."""
    info = esp.info
    if info is None:
        return None
    return {
        "name": info.name or None,
        "chip": info.chip.name,
        "chip_rev": info.chip_rev,
        "mac": info.mac,
        "firmware": ".".join(map(str, info.fw_version)),
        "protocol": info.protocol,
        "flash_mb": info.flash_mb,
        "gpio_count": info.gpio_count,
        "capabilities": caps_list(info.caps),
    }
