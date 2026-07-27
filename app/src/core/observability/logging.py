"""Idempotent console/JSON logging with optional bounded UTF-8 file storage."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SENSITIVE_LOG_VALUE = re.compile(
    r"(?i)\b(authorization|api[_-]?key|access[_-]?token|password|captcha(?:_bypass)?|"
    r"secret|x-hwid)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|"
    r"(?:bearer|apikey|basic)\s+[^\s,;]+|[^\s,;]+)"
)
_STANDARD_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _redact_text(value: str) -> str:
    return _SENSITIVE_LOG_VALUE.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = _redact_text(record.getMessage())
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }
        if message.startswith("event="):
            for item in shlex.split(message):
                key, separator, value = item.partition("=")
                if separator and key:
                    payload[key] = value
        else:
            payload["message"] = message.replace("\n", " ")[:500]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact_text(super().format(record))


class AnsiStrippingFormatter(RedactingFormatter):
    """Keep file output plain even when the terminal human trace is colored."""

    def format(self, record: logging.LogRecord) -> str:
        return _ANSI_ESCAPE.sub("", super().format(record))


class _JsonTerminalFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not bool(getattr(record, "soorin_human_trace", False))


class SafeRotatingFileHandler(RotatingFileHandler):
    """Report a bounded safe rotation error without raising into request handling."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._rotation_error_reported = False

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        if not self._rotation_error_reported:
            self._rotation_error_reported = True
            try:
                sys.stderr.write("event=log_file_rotation_failed path_kind=runtime_log\n")
            except Exception:
                pass


def _level(value: Any, default: int = logging.INFO) -> int:
    return getattr(logging, str(value or "").upper(), default)


def _fingerprint(settings: object) -> tuple[Any, ...]:
    return (
        getattr(settings, "log_level", "INFO"),
        getattr(settings, "log_format", "console"),
        bool(getattr(settings, "log_file_enabled", True)),
        getattr(settings, "log_file_path", "data/runtime/logs/soorin-copilot.log"),
        getattr(settings, "log_file_level", "INFO"),
        int(getattr(settings, "log_file_max_bytes", 20971520)),
        int(getattr(settings, "log_file_backup_count", 10)),
    )


def configure_application_logging(
    settings: object,
    *,
    target_logger: logging.Logger | None = None,
) -> None:
    """Configure terminal plus one optional rotating file handler exactly once."""
    target = target_logger or logging.getLogger()
    fingerprint = _fingerprint(settings)
    current_handlers = [
        handler for handler in target.handlers if getattr(handler, "_soorin_managed", False)
    ]
    if getattr(target, "_soorin_logging_fingerprint", None) == fingerprint and current_handlers:
        return

    for handler in current_handlers:
        target.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    target.setLevel(_level(getattr(settings, "log_level", "INFO")))
    if target_logger is not None:
        target.propagate = False

    terminal = logging.StreamHandler(sys.stdout)
    terminal._soorin_managed = True  # type: ignore[attr-defined]
    terminal._soorin_handler_kind = "terminal"  # type: ignore[attr-defined]
    if getattr(settings, "log_format", "console") == "json":
        terminal.setFormatter(JsonLogFormatter())
        terminal.addFilter(_JsonTerminalFilter())
    else:
        terminal.setFormatter(RedactingFormatter(_STANDARD_FORMAT))
    target.addHandler(terminal)

    if bool(getattr(settings, "log_file_enabled", True)):
        try:
            path = Path(
                str(getattr(settings, "log_file_path", "data/runtime/logs/soorin-copilot.log"))
            ).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            file_handler = SafeRotatingFileHandler(
                path,
                maxBytes=max(1024, int(getattr(settings, "log_file_max_bytes", 20971520))),
                backupCount=max(0, int(getattr(settings, "log_file_backup_count", 10))),
                encoding="utf-8",
                delay=True,
            )
            file_handler._soorin_managed = True  # type: ignore[attr-defined]
            file_handler._soorin_handler_kind = "runtime_file"  # type: ignore[attr-defined]
            file_handler.setLevel(_level(getattr(settings, "log_file_level", "INFO")))
            file_handler.setFormatter(AnsiStrippingFormatter(_STANDARD_FORMAT))
            target.addHandler(file_handler)
            target.info(
                "event=log_file_initialized path_kind=runtime_log max_bytes=%s backup_count=%s",
                file_handler.maxBytes,
                file_handler.backupCount,
            )
        except (OSError, ValueError, TypeError):
            target.warning(
                "event=log_file_initialization_failed path_kind=runtime_log error_class=configuration_or_io"
            )

    target._soorin_logging_fingerprint = fingerprint  # type: ignore[attr-defined]
