#!/usr/bin/env python3
"""Export a country x month digital-engagement release product for one organisation.

Dry-run first (prints counts and the path it would write, writes nothing)::

    docker exec imports_container python3 /app/python/scripts/export_release_product.py \\
        --org <organisation esid> --teams <team esid>,<team esid> --countries PK,IN

Then write the file::

    docker exec imports_container python3 /app/python/scripts/export_release_product.py \\
        --org <organisation esid> --teams <team esid>,<team esid> --countries PK,IN --apply [--out <path>]

What the file contains, the counting rules and the organisation settings it
reads are documented in ``asset_management/jobs/export_release_product.py``.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from asset_management.jobs import export_release_product as job  # noqa: E402
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def _csv(value: str) -> list[str]:
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--org", required=True, help="organisation esid (de_asset_organization document id)")
    parser.add_argument("--teams", required=True, help="comma-separated team esids to include (must belong to --org)")
    parser.add_argument("--countries", required=True, help="comma-separated ISO 3166-1 alpha-2 codes to publish")
    parser.add_argument("--apply", action="store_true", help="write the file (default is a dry run that writes nothing)")
    parser.add_argument(
        "--out", default=None, help="output path (default: %s/release-product_<org>_<month>.json)" % job.DEFAULT_OUT_DIR
    )
    parser.add_argument("--test-data", action="store_true", help="mark the release as synthetic (manifest.testData)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch is not reachable; nothing exported")
        return 1
    return job.run(
        es,
        org_esid=args.org,
        team_esids=_csv(args.teams),
        countries=_csv(args.countries),
        apply=args.apply,
        out_path=args.out,
        test_data=args.test_data,
    )


if __name__ == "__main__":
    sys.exit(main())
