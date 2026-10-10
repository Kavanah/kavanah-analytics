from flask import Blueprint

report_scheduler_bp = Blueprint(
    "report_scheduler",
    __name__,
    template_folder="../../templates",
    static_folder="../../static",
    url_prefix="/report-scheduler",
)

from . import routes  # noqa: E402, F401
