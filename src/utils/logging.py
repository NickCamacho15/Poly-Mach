"""
Structured logging configuration for the bot.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog


def _coerce_level(log_level: str) -> int:
    if not log_level:
        return logging.INFO
    return getattr(logging, log_level.upper(), logging.INFO)


def configure_logging(
    log_level: str = "INFO",
    log_file: str = "logs/bot.log",
    log_json: bool = False,
) -> None:
    level = _coerce_level(log_level)

    handlers = [logging.StreamHandler(sys.stdout)]
    log_path = log_file.strip() if log_file else ""
    if log_path:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        handlers.append(logging.FileHandler(log_path))

    logging.basicConfig(level=level, handlers=handlers, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    processors = [
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if log_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )
