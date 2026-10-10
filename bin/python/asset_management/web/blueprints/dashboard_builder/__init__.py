from flask import Blueprint

dashboard_bp = Blueprint(
    "dashboard_builder",
    __name__,
    template_folder="../../templates",
    static_folder="../../static",
    url_prefix="/dashboard-builder",
)

from . import routes  # noqa: E402, F401
