from flask import Blueprint

publish_global_church_bp = Blueprint(
    "publish_global_church",
    __name__,
    template_folder="../../templates",
    static_folder="../../static",
    url_prefix="/publish-global-church",
)

from . import routes  # noqa: E402, F401
