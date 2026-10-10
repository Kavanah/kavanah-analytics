from flask import Blueprint

field_catalog_bp = Blueprint(
    "field_catalog",
    __name__,
    template_folder="../../templates",
    static_folder="../../static",
    url_prefix="/field-catalog",
)

from . import routes  # noqa: E402, F401
