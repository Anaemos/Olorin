"""
core/logging_config.py — Structured logging setup for Olorin.

One place to configure logging so every module gets consistent formatting
instead of ad-hoc print() calls. Logs to both console and a rotating-ish
flat file under the project root (logs/olorin.log) for later inspection —
useful for debugging routing decisions after the fact without re-running
anything.
"""

import logging
import os

import config

_LOG_DIR = os.path.join(config.PROJECT_ROOT, "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "olorin.log")

_configured = False


def get_logger(name: str) -> logging.Logger:
    global _configured

    if not _configured:
        os.makedirs(_LOG_DIR, exist_ok=True)

        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        root = logging.getLogger("olorin")
        root.setLevel(logging.INFO)
        root.addHandler(file_handler)
        root.addHandler(console_handler)

        _configured = True

    return logging.getLogger(f"olorin.{name}")
