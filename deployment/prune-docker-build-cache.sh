#!/usr/bin/env bash
# =============================================================================
# | prune-docker-build-cache.sh — bound the Docker build cache
# -----------------------------------------------------------------------------
# Every image build on a VM (start-secrets-container.sh rebuilds de-imports,
# step6-nginx-setup.sh builds nginx-reverse-proxy with --no-cache, and
# 01_build_images.sh when run on a box) leaves BuildKit cache behind, and
# nothing ever removed it. Prod had 19.5 GB across 172 entries on 2026-10-12,
# more than 40x the Elasticsearch data on the same disk. Unbounded growth on
# the ES box matters: at the 95% flood-stage watermark ES sets every index
# read-only and imports stop for every org at once.
#
# This keeps the most recently used cache up to PRUNE_KEEP_STORAGE (so the
# next rebuild still hits the layer cache) and drops dangling images (the
# untagged previous :latest left by each rebuild). Tagged images are never
# touched, so rollback tags such as pre_2 survive. Running
# containers are never affected and nothing restarts.
#
# Called at the end of each build step; also safe to run by hand:
#   bash deployment/prune-docker-build-cache.sh
#   PRUNE_KEEP_STORAGE=4GB bash deployment/prune-docker-build-cache.sh
#
# Exit 0 unless docker itself is unusable; callers treat a failure here as
# a warning, never as a failed deploy.
# =============================================================================
set -euo pipefail

KEEP="${PRUNE_KEEP_STORAGE:-2GB}"

if ! command -v docker >/dev/null 2>&1; then
    echo "[WARN]    prune-docker-build-cache: docker not on PATH; skipping" >&2
    exit 0
fi

echo "[INFO]    Docker disk use before prune:"
docker system df 2>/dev/null | sed 's/^/          /' || true

# Docker 28 renamed --keep-storage to --reserved-space; the old flag is still
# accepted as an alias, so use whichever this daemon's CLI documents.
if docker builder prune --help 2>&1 | grep -q -- '--reserved-space'; then
    docker builder prune --force --reserved-space "$KEEP"
else
    docker builder prune --force --keep-storage "$KEEP"
fi

# Dangling only (no -a): untagged leftovers, never a tagged image.
docker image prune --force

echo "[INFO]    Docker disk use after prune (build cache kept ≤ $KEEP):"
docker system df 2>/dev/null | sed 's/^/          /' || true
