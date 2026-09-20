"""Logging configuration.

Three levers, because three different things go wrong:

* `VNODE_LOG_LEVEL` - our own code. INFO narrates the lifecycle and anything a
  person caused; DEBUG explains every routing decision.
* `VNODE_TRACE_FRAMES` - dumps every frame crossing either socket, with a hex
  prefix. This is the tool for "the app connects and shows nothing": it is the
  only way to see what actually went over the wire.
* `VNODE_MESHTASTIC_LOG_LEVEL` - the upstream library, kept separate because it
  is very chatty and is usually not the thing that is broken.

A rotating file is used when `VNODE_LOG_FILE` is set, so a Pi left running for
months does not fill its card.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

# Below DEBUG: per-frame wire dumps, which would drown out everything else.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")

FORMAT = "%(asctime)s %(levelname)-5s [%(threadName)-12.12s] %(name)-28s %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def trace(logger: logging.Logger, msg: str, *args) -> None:
    if logger.isEnabledFor(TRACE):
        logger.log(TRACE, msg, *args)


def hexdump(data: bytes, limit: int = 48) -> str:
    """First bytes as hex, so a frame can be compared against a capture."""
    head = data[:limit].hex(" ")
    return f"{head}{'…' if len(data) > limit else ''} ({len(data)}B)"


def configure(
    *,
    level: str = "INFO",
    trace_frames: bool = False,
    meshtastic_level: str = "WARNING",
    log_file: Path | str | None = None,
    max_bytes: int = 8_000_000,
    backups: int = 3,
    console: bool = True,
) -> None:
    root_level = TRACE if trace_frames else getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler()] if console else []
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
        )

    if not handlers:
        handlers.append(logging.StreamHandler())  # never log to nowhere
    formatter = logging.Formatter(FORMAT, datefmt=DATEFMT)
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.setLevel(root_level)
        root.addHandler(handler)
    root.setLevel(root_level)

    # The library's own logging is on its own dial: it logs every received frame
    # at DEBUG, which buries ours.
    logging.getLogger("meshtastic").setLevel(
        getattr(logging, meshtastic_level.upper(), logging.WARNING)
    )
    # uvicorn's access log is one line per web-UI poll; useful only when the web
    # UI itself is the problem.
    if root_level > logging.DEBUG:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    if trace_frames:
        logging.getLogger(__name__).info(
            "vnode: frame tracing is ON - every frame on both sockets is logged"
        )
