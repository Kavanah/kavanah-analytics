from flask import Blueprint

analytics_bp = Blueprint(
    "analytics",
    __name__,
    template_folder="../../templates",
    static_folder="../../static",
)

from . import routes  # noqa: E402, F401
