from flask import Blueprint

public_report_bp = Blueprint(
    "public_report",
    __name__,
    template_folder="../../templates",
    static_folder="../../static",
    url_prefix="/public",
)

from . import routes  # noqa: E402, F401
