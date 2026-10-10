"""superuser page for personal signup links."""

from flask import Blueprint

signup_links_bp = Blueprint(
    "signup_links",
    __name__,
    template_folder="../../templates",
    static_folder="../../static",
    url_prefix="/signup-links",
)

from . import routes  # noqa: E402, F401
