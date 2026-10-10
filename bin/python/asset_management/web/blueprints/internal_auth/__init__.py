from flask import Blueprint

internal_auth_bp = Blueprint(
    "internal_auth",
    __name__,
)

from . import routes  # noqa: E402, F401
