from flask import Blueprint

connections_bp = Blueprint(
    "connections",
    __name__,
    template_folder="../../templates",
    static_folder="../../static",
    url_prefix="/connections",
)

from . import routes  # noqa: E402, F401
