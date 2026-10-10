from flask import Blueprint

data_explorer_bp = Blueprint(
    "data_explorer",
    __name__,
    template_folder="../../templates",
    static_folder="../../static",
    url_prefix="/data-explorer",
)

from . import routes  # noqa: E402, F401
