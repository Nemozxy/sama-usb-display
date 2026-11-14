"""Logging setup: one place that configures console + file logging for the tool.

Every session gets a timestamped log file so a hardware run can be reconstructed
byte-for-byte afterwards.  Raw traffic is logged at DEBUG as a hexdump.
"""

from __future__ import annotations

import logging
import os
import sys
import time

DEFAULT_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "logs")

_configured = False


def setup(level: str = "INFO", log_dir: str | None = None,
          log_file: str | None = None, quiet: bool = False) -> str:
    """Configure root logging.  Returns the path of the log file.

    Parameters
    ----------
    level
        ``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``.
    log_file
        Explicit file path; otherwise ``<log_dir>/sama-<timestamp>.log``.
    """
    global _configured
    lvl = getattr(logging, str(level).upper(), logging.INFO)

    if log_file is None:
        d = log_dir or DEFAULT_LOG_DIR
        os.makedirs(d, exist_ok=True)
        log_file = os.path.join(d, time.strftime("sama-%Y%m%d-%H%M%S.log"))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-16s %(message)s", "%H:%M:%S")

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if not quiet:
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(lvl)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    # make the console able to render the obfuscated identifiers we cite
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

    _configured = True
    logging.getLogger("sama").info("logging to %s (level %s)",
                                   os.path.abspath(log_file), logging.getLevelName(lvl))
    return os.path.abspath(log_file)


def is_configured() -> bool:
    return _configured


def ensure_configured(level: str = "INFO", **kw) -> str:
    """Idempotent setup, for library use."""
    return setup(level, **kw) if not _configured else ""
