"""Logging filters for the project.

 Phase 3: ``CrlfScrubFilter`` neutralizes log-injection by replacing
CR/LF/NUL bytes in formatted log records with printable escape sequences
before they reach any handler. Every formatted log record passes through
exactly one filter pass — short logs (no CR/LF/NUL) are untouched.

Why a runtime filter rather than per-call-site sanitization:

* The project has ~165 log statements that interpolate user-provided values
  via f-strings (audit log lines, error context with usernames/asset names,
  etc.). Migrating every one of them to ``%``-formatting with explicit
  argument lists is mechanical churn with high regression risk.
* A single root-logger filter sanitizes every log record in the process,
  including third-party libraries, without touching any call site.
* CodeQL ``py/log-injection`` is suppressed at the rule level in
  ``.github/codeql/codeql-config.yml``, citing this filter as the mitigation.
  The rule is noisy precisely because the static analyzer can't see the
  runtime filter — the filter is real, the noise isn't useful.

The CR/LF threat model is "an attacker injects ``\\n[FAKE LOG LINE]\\n``
into a logged value to forge log entries." Replacing CR/LF/NUL with their
printable escape forms (``\\r``, ``\\n``, ``\\x00``) lets operators see what
the original bytes were without the parser-level forgery.
"""

from __future__ import annotations

import logging
import re


_DANGEROUS_TO_ESCAPED = str.maketrans(
    {
        "\r": "\\r",
        "\n": "\\n",
        "\x00": "\\x00",
    }
)


class CrlfScrubFilter(logging.Filter):
    """Replace CR/LF/NUL in the formatted log message with printable escapes.

    Operates on ``record.getMessage()`` output (after ``%``/``f``-string
    formatting has already happened). Mutates ``record.msg`` to the cleaned
    string and clears ``record.args`` so the formatter does not double-apply
    the format placeholders.

    The filter never drops records (always returns ``True``).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # If formatting itself fails, leave the record alone — the
            # default handler error path will surface the real issue.
            return True

        if "\r" not in message and "\n" not in message and "\x00" not in message:
            return True

        record.msg = message.translate(_DANGEROUS_TO_ESCAPED)
        record.args = None
        return True


# credential-bearing names. Graph calls carry the token in the URL
# query, and a requests error repeats the full URL ("500 Server Error ... for
# url: ...?access_token=EAA..."), so any log line holding that error held the
# token.
_SECRET_NAMES = r"(?:access_token|input_token|fb_exchange_token|client_secret|appsecret_proof|refresh_token|id_token)"
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # query string / form body: access_token=VALUE (also URL-encoded =)
    (re.compile(rf"(?i)\b({_SECRET_NAMES})(=|%3D)[^&\s\"'#<>]+"), r"\1\2[REDACTED]"),
    # JSON / dict repr: "access_token": "VALUE"
    (re.compile(rf"(?i)([\"']{_SECRET_NAMES}[\"']\s*:\s*[\"'])[^\"']+([\"'])"), r"\1[REDACTED]\2"),
    # Authorization: Bearer VALUE
    (re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
)


def redact_secrets(text: str) -> str:
    """Return ``text`` with credential values replaced by ``[REDACTED]``."""
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class SecretScrubFilter(logging.Filter):
    """Redact credential values from the formatted message and its traceback.

    The traceback matters as much as the message: ``logging.exception`` on a
    requests error prints the exception text, URL and token included. The
    traceback is rendered here once, redacted, and stored in
    ``record.exc_text``, which ``Formatter.format`` uses instead of
    re-rendering ``exc_info``.

    The filter never drops records (always returns ``True``).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 — leave unformattable records to the handler's error path
            return True

        cleaned = redact_secrets(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = None

        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_secrets(record.stack_info)
        return True


_PROCESS_FILTERS: tuple[type[logging.Filter], ...] = (CrlfScrubFilter, SecretScrubFilter)


def install_filter_on_all_handlers() -> None:
    """Attach ``CrlfScrubFilter`` and ``SecretScrubFilter`` to every existing handler.

    Filters attached to loggers only run for records emitted via that
    logger's own ``log()``/``info()``/``debug()`` methods — they are NOT
    consulted for records that bubble up from descendant loggers via
    propagation (Python ``logging`` docs, "Filter objects"). Filters on
    handlers, in contrast, run for every record that reaches that handler
    regardless of which logger originated it.

    Walk the entire ``logging.Logger.manager.loggerDict`` plus the root
    logger and attach one of each process filter to each unique handler. Any
    handler attached after this call will not be covered, so callers must
    invoke this AFTER all logging setup has run (``basicConfig``, named
    loggers with custom handlers, etc.).

    Idempotent — a handler that already carries a filter of a given type is
    not given a second one, so this can be called multiple times across test
    runs.
    """
    seen: set[int] = set()

    def _attach(handler: logging.Handler) -> None:
        if id(handler) in seen:
            return
        seen.add(id(handler))
        for filter_type in _PROCESS_FILTERS:
            if not any(isinstance(f, filter_type) for f in handler.filters):
                handler.addFilter(filter_type())

    for handler in logging.getLogger().handlers:
        _attach(handler)
    for name in list(logging.Logger.manager.loggerDict):
        logger = logging.getLogger(name)
        for handler in getattr(logger, "handlers", []) or []:
            _attach(handler)
