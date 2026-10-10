#!/usr/bin/env bash
# Validate that a secret value is non-empty, not a yq null sentinel, and
# at least 32 characters.
#
# Source this file then call:
#   validate_secret "$value" "SECRET_KEY"
#
# Returns 0 if valid, 1 with a stderr message if not. Mirrors the rules in
# bin/python/asset_management/web/secret_validation.py — keep both in sync.
#
# Background: the 2026-04-23 prod incident ran Flask with SECRET_KEY="null"
# because yq prints the literal string "null" for a missing yaml key, and
# nothing in the deploy path refused it.

# Guard against double-sourcing.
if [[ -n "${_VALIDATE_SECRET_SH_SOURCED:-}" ]]; then
    return 0 2>/dev/null || true
fi
_VALIDATE_SECRET_SH_SOURCED=1

_MIN_SECRET_LENGTH=32

validate_secret() {
    local value="${1-}"
    local name="${2:-SECRET}"

    if [[ -z "$value" ]]; then
        echo "ERROR: $name is empty. Set it in master-config.yaml or the environment." >&2
        return 1
    fi

    case "$value" in
        null|None|"dev-secret-key-change-in-production")
            echo "ERROR: $name is set to forbidden sentinel value '$value'. This usually means the yaml key is missing — set it in master-config.yaml to a strong random value (openssl rand -hex 32)." >&2
            return 1
            ;;
    esac

    if (( ${#value} < _MIN_SECRET_LENGTH )); then
        echo "ERROR: $name is too short (${#value} chars; minimum is ${_MIN_SECRET_LENGTH}). Generate with: openssl rand -hex 32" >&2
        return 1
    fi

    return 0
}
