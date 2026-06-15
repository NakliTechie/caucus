"""Redacted logging: logs never carry bodies, keys, or source.

The discipline is structural: callers log *events* with whitelisted metadata fields
only (request id, turn type, model, status, latency, token counts). There is no code
path that accepts a prompt or completion body into a log record. ``key_fingerprint``
is the only key-derived value ever emitted, and it is a non-reversible digest.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import threading
from collections import deque

_CONFIGURED = False

# ---- in-process log ring buffer (console "Logs" tab; GET /v1/logs) ----------------------
# A bounded, same-origin view of what the daemon is logging — so the LiteLLM per-call logs and
# the synth (MOA) trace can be watched in the browser, not just `tail -f`. It mirrors
# the loggers' current verbosity: quiet metadata only in normal mode; the full debug stream
# under `caucus start --debug`. Capped, in-memory, never written to disk by this buffer.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _RingBuffer:
    def __init__(self, cap: int = 2000) -> None:
        self._buf: deque[dict] = deque(maxlen=cap)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, entry: dict) -> None:
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._buf.append(entry)

    def tail(self, since: int = 0, limit: int = 600) -> list[dict]:
        with self._lock:
            out = [e for e in self._buf if e["seq"] > since]
        return out[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


_RING = _RingBuffer()


def _source_for(name: str) -> str:
    if name.startswith("LiteLLM"):
        return "litellm"
    if "synth" in name:
        return "synth"
    return "caucus"


class _RingHandler(logging.Handler):
    """Captures records into the ring (metadata + message text only — no record args retained)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = _ANSI.sub("", record.getMessage())
        except Exception:
            return
        _RING.add({"ts": record.created, "level": record.levelname,
                   "source": _source_for(record.name), "logger": record.name, "msg": msg[:2000]})


_RING_HANDLER = _RingHandler()
_RING_HANDLER.setLevel(logging.DEBUG)


def attach_ring(*logger_names: str) -> None:
    """Tee the named loggers into the ring buffer (idempotent). Used for the LiteLLM loggers,
    which live outside the ``caucus`` tree."""
    for n in logger_names:
        lg = logging.getLogger(n)
        if _RING_HANDLER not in lg.handlers:
            lg.addHandler(_RING_HANDLER)


def ring_tail(since: int = 0, limit: int = 600) -> list[dict]:
    return _RING.tail(since, limit)


def ring_clear() -> None:
    _RING.clear()


def configure(level: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    lvl = (level or os.environ.get("CAUCUS_LOG_LEVEL", "INFO")).upper()
    fmt = logging.Formatter("%(asctime)s caucus %(levelname)s %(name)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file = os.environ.get("CAUCUS_LOG_FILE")
    if log_file:  # redacted lines only — `caucus logs` tails this
        try:
            handlers.append(logging.FileHandler(log_file))
        except Exception:
            pass
    for h in handlers:
        h.setFormatter(fmt)
    root = logging.getLogger("caucus")
    root.handlers[:] = handlers + [_RING_HANDLER]  # tee caucus + synth_engine logs into the ring
    root.setLevel(getattr(logging, lvl, logging.INFO))
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure()
    return logging.getLogger(f"caucus.{name}")


def key_fingerprint(key: str | None) -> str:
    """A short, non-reversible fingerprint for recognising a key (never the key)."""
    if not key:
        return "none"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:8]}…{digest[-4:]}"


def event(logger: logging.Logger, msg: str, /, **fields: object) -> None:
    """Log a structured event. Only whitelisted scalar metadata — never bodies.

    A guard rejects suspiciously large values so a prompt/completion can never be
    smuggled into a log line through a field.
    """
    safe = []
    for k, v in fields.items():
        text = str(v)
        if len(text) > 200:
            text = f"<{len(text)} chars elided>"
        safe.append(f"{k}={text}")
    logger.info("%s %s", msg, " ".join(safe)) if safe else logger.info(msg)
