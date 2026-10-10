from flask import Blueprint

restore_backup_bp = Blueprint(
    "restore_backup",
    __name__,
    template_folder="../../templates",
    static_folder="../../static",
    url_prefix="/restore-backup",
)

from . import routes  # noqa: E402, F401
