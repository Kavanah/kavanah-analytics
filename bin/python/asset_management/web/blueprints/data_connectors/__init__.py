from flask import Blueprint

data_connectors_bp = Blueprint(
    "data_connectors",
    __name__,
    template_folder="../../templates",
    static_folder="../../static",
    url_prefix="/data-connectors",
)

from . import routes  # noqa: E402, F401
