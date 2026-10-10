#!/usr/bin/env bash
#
# source-ssm-secrets.sh — fetch the /km/<env>/* SSM tree and EXPORT the secrets
# into the current shell, for the DEPLOY layer.
#
# WHY this exists separately from start-secrets-container.sh:
#   The wrapper rewrites deployment/.env and recreates imports_container — it
#   injects the CONTAINER's secrets and deliberately SKIPS the deploy-layer
#   secrets (kibana-system-password) the container never reads. step3-app-stack.sh
#   is the OTHER consumer: it bakes kibana_system's password literally into
#   kibana.yml and reads enc_key/flask/es/github when writing deployment/.env.
#   Once Phase 3 strips those VALUES from master-config.yaml, step3 must get them
#   from somewhere — this lib is that somewhere. SOURCE it before running step3:
#
#       SSM_ENV=dev source deployment/lib/source-ssm-secrets.sh \
#         && bash deployment/step3-app-stack.sh
#
#   It only EXPORTS env vars (no file writes, no container actions), so it's safe
#   to source in any deploy shell. step3 reads each secret env-first, so the
#   exported values win and a master-config with the values stripped still works.
#
# IDENTITY KNOBS (mirror start-secrets-container.sh — same per-env reader):
#   SSM_ENV     which /km/<env>/ tree to read (default: staging)
#   SSM_REGION  AWS region (default: us-west-2)
#   SSM_PROFILE AWS profile; "" drops --profile (prod's [default] reader).
#               Unset defaults to "bootstrap" (staging, in root's ~/.aws).
#   SSM_SUDO    1 (default) => sudo aws (staging); 0 => bare aws as the user.
#
# This file is SOURCED, not executed — it must not `exit` on error (that would
# kill the operator's shell). It returns non-zero and prints to stderr instead.

# ── Configuration ─────────────────────────────────────────────────────────────
_SSM_ENV="${SSM_ENV:-staging}"
_SSM_REGION="${SSM_REGION:-us-west-2}"
_SSM_PROFILE_NAME="${SSM_PROFILE-bootstrap}"   # no colon: explicit "" is honored
_SSM_SUDO="${SSM_SUDO:-1}"
_SSM_PATH="/km/${_SSM_ENV}/"

# ── Param (kebab, relative to _SSM_PATH) → exported env var ───────────────────
# Superset of the wrapper's table: INCLUDES kibana-system-password (the
# deploy-layer secret step3 bakes into kibana.yml). kibana-token is intentionally
# absent — confirmed dead on both envs.
declare -A _SSM_PARAM_TO_ENV=(
    ["deam/enc-key"]="ENC_KEY"
    ["deam/enc-key-previous"]="ENC_KEY_PREVIOUS" # optional, rotation window only
    ["deam/flask-secret-key"]="FLASK_SECRET_KEY"
    ["deam/github-read-only-token"]="GITHUB_TOKEN"
    ["elasticsearch/elastic-password"]="ES_ADMIN_PASS"
    ["elasticsearch/de-es-admin-api-key"]="ES_ADMIN_API_KEY" # write client; optional seeds it
    ["elasticsearch/kibana-system-password"]="KIBANA_SYS_PASS"
)

# ── Fetch ─────────────────────────────────────────────────────────────────────
_ssm_source_main() {
    command -v aws >/dev/null 2>&1 || { echo "[ERROR] aws CLI not found" >&2; return 1; }

    local -a aws_cmd=()
    [[ "$_SSM_SUDO" == "1" ]] && aws_cmd+=(sudo)
    aws_cmd+=(aws)
    [[ -n "$_SSM_PROFILE_NAME" ]] && aws_cmd+=(--profile "$_SSM_PROFILE_NAME")

    local profile_desc="${_SSM_PROFILE_NAME:-<default>}"
    echo "[INFO] Sourcing SSM secrets from '$_SSM_PATH' (profile '$profile_desc', region '$_SSM_REGION', sudo=$_SSM_SUDO)..." >&2

    local out
    out=$("${aws_cmd[@]}" ssm get-parameters-by-path \
        --region "$_SSM_REGION" \
        --path "$_SSM_PATH" \
        --recursive \
        --with-decryption \
        --query "Parameters[].[Name,Value]" \
        --output text 2>&1) || {
        echo "[ERROR] SSM fetch failed (identity '$profile_desc' may lack ssm:GetParametersByPath/kms:Decrypt on ${_SSM_PATH}*): $out" >&2
        return 1
    }
    [[ -n "$out" ]] || { echo "[ERROR] SSM returned no params under ${_SSM_PATH}" >&2; return 1; }

    local exported=0 name value rel env_name
    while IFS=$'\t' read -r name value; do
        [[ -z "$name" ]] && continue
        rel="${name#"$_SSM_PATH"}"
        env_name="${_SSM_PARAM_TO_ENV[$rel]:-}"
        [[ -z "$env_name" ]] && continue   # skip params this layer doesn't consume
        export "$env_name=$value"
        exported=$((exported + 1))
    done <<< "$out"

    [[ -n "${ENC_KEY:-}" ]] || { echo "[ERROR] ENC_KEY not exported from SSM — aborting (it decrypts every connector token)." >&2; return 1; }
    echo "[OK] Exported $exported deploy-layer secrets from SSM (${_SSM_PATH}*)." >&2
    return 0
}

_ssm_source_main
