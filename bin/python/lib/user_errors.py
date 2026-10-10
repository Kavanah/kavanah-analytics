"""Exceptions whose message is written for the person using the app.

Kept free of Flask so non-web modules (load-test profile parsing, CLI tools)
can raise them; ``lib.errors`` re-exports them for the web layer.
"""

from __future__ import annotations


class UserFacingError(Exception):
    """An exception whose message was written for the person using the app.

    domain exceptions (wizard, menu, template, snapshot, schedule,
    ...) are raised with our own validation text, and routes return that text
    to the caller. They return ``public_message``, never ``str(e)``: the
    attribute names the contract ("this text is meant for users"), and
    ``str()`` of an exception is exactly what a stack-trace leak looks like.
    Raise sites must pass constant or user-supplied text only — never the text
    of another exception; ``ERR-001`` guards the services.
    """

    def __init__(self, message: str = "", *args: object) -> None:
        super().__init__(message, *args)
        self.public_message = message


class InvalidRequestError(UserFacingError, ValueError):
    """Bad input, described to the user in our own words.

    Subclasses ``ValueError`` so callers that already catch ValueError keep
    working; routes catch this type, so a library ValueError (JSON decode,
    int() of junk, ...) reaches the generic error path instead of the user.
    """


class RequestedItemNotFoundError(UserFacingError, FileNotFoundError):
    """A named backup / snapshot / extraction does not exist. Subclasses
    ``FileNotFoundError`` for the same reason ``InvalidRequestError``
    subclasses ``ValueError``."""
