"""Entry point: ``python -m zafro_bridge``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .bridge import Bridge
from .config import Settings
from .version import VERSION

_LEVELS = {
    "trace": logging.DEBUG,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "fatal": logging.CRITICAL,
}


def _configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=_LEVELS.get(level_name.lower(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # aiohttp/aiomqtt are chatty at DEBUG; keep them one notch quieter.
    logging.getLogger("aiohttp").setLevel(logging.INFO)


async def _main() -> None:
    settings = Settings.from_environment(VERSION)
    _configure_logging(settings.log_level)
    logging.getLogger(__name__).info("Zafro Bridge %s starting", VERSION)

    bridge = Bridge(settings)
    bridge_task = asyncio.create_task(bridge.run(), name="bridge")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bridge_task.cancel)

    try:
        await bridge_task
    except asyncio.CancelledError:
        logging.getLogger(__name__).info("Zafro Bridge stopping")


def _run() -> int:
    """Run the bridge to completion and return the process exit code.

    A startup failure - a missing/unreadable TLS certificate, the device
    port already in use, any other exception that escapes ``bridge.run()``
    before or after logging is configured - must be logged clearly (through
    the same logger as every other bridge message, not a bare traceback that
    could scroll past unnoticed) and must exit non-zero. s6 only stops
    retrying and surfaces the add-on as failed when the service actually
    exits with an error; a process that logs a traceback but exits 0 would
    otherwise leave the add-on looking "started" while doing nothing.
    """
    try:
        asyncio.run(_main())
    except Exception:  # noqa: BLE001 - last-resort boundary before process exit
        logging.getLogger(__name__).exception("Zafro Bridge stopped because of an unexpected error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_run())
