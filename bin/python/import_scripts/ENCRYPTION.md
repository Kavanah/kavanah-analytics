# Service Account Encryption Guide

> **Deprecated (2026-07-14, LIB-001).** The service-account-file auth pattern
> this guide describes is retired. YouTube, Google Ads, and GA4 are all
> OAuth-only now, so no `*-sa.json` service
> account files exist on any environment. The `load_encrypted_service_account()`
> helper was removed as dead code. The generic `encrypt_json_file()` /
> `decrypt_json_file()` / `encrypt()` / `decrypt()` helpers in
> `lib/encryption.py` are still current and used for OAuth tokens
> (e.g. `youtube-oauth-client.json.enc`). This doc is retained for historical
> context only and should not be followed for new work.

## Overview

This document explains how to encrypt service account JSON keys for enhanced security. Encrypted service accounts are stored as `.enc` files and automatically decrypted when needed by import scripts.

### Security Benefits

| Aspect | Plaintext Keys | Encrypted Keys |
|--------|----------------|----------------|
| **At Rest** | Readable by anyone with file access | Requires encryption key to read |
| **Accidental Exposure** | Keys visible in backups, logs | Encrypted data safe if exposed |
| **File Permissions** | Relies only on 600 permissions | Encryption + 600 permissions |
| **Compliance** | Basic security | Enhanced data protection |

---

## Quick Start

### Encrypt All Service Accounts

```bash
# SSH into server
ssh -i ~/.ssh/id_ed25519 ubuntu@<server-ip>

# Preview what would be encrypted (dry run)
sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --all --dry-run

# Encrypt all service accounts
sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --all

# Optionally delete plaintext files after encryption (use with caution!)
sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --all --delete-plaintext
```

### Encrypt Specific Service Account

```bash
# YouTube Analytics
sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --file /app/reporting_configs/youtube-analytics-sa.json

# Google Ads
sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --file /app/reporting_configs/google-ads-sa.json

# Google Analytics (GA4)
sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --file /app/data/google_analytics/ga_service_account.json
```

---

## How It Works

### File Structure

**Before encryption**:
```
/app/reporting_configs/
├── youtube-analytics-sa.json          (plaintext, 600 permissions)
└── google-ads-sa.json                 (plaintext, 600 permissions)
```

**After encryption**:
```
/app/reporting_configs/
├── youtube-analytics-sa.json          (optional: can be deleted)
├── youtube-analytics-sa.json.enc      (encrypted, 600 permissions) ✅
├── google-ads-sa.json                 (optional: can be deleted)
└── google-ads-sa.json.enc             (encrypted, 600 permissions) ✅
```

### Automatic Detection

Import scripts automatically detect and use encrypted files:

1. **Check for `.enc` file first**: If `/app/reporting_configs/youtube-analytics-sa.json.enc` exists, use it
2. **Fall back to plaintext**: If only `/app/reporting_configs/youtube-analytics-sa.json` exists, use that (with warning)
3. **Error if neither exists**: Raise FileNotFoundError

This allows **gradual migration** - you can encrypt service accounts one at a time without breaking existing imports.

### Encryption Method

- **Algorithm**: Fernet (symmetric encryption)
- **Key Source**: AWS SSM `/km/<env>/deam/enc-key` → `ENC_KEY` in imports_container; legacy fallback `enc_key` in master-config.yaml on hosts not yet stripped
- **Key Rotation**: `ENC_KEY_PREVIOUS` (optional) lets old ciphertexts decrypt during a rotation; see "Key Rotation" below
- **Cipher**: AES-128-CBC with HMAC authentication

---

## Step-by-Step Migration

### Phase 1: Test with Dry Run

Preview what will be encrypted without making changes:

```bash
sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --all --dry-run
```

**Expected output**:
```
============================================================
Service Account Encryption Utility
============================================================
[DRY RUN MODE] No files will be modified

Encrypting all service accounts:

YOUTUBE: /app/reporting_configs/youtube-analytics-sa.json
[DRY RUN] Would encrypt: /app/reporting_configs/youtube-analytics-sa.json → /app/reporting_configs/youtube-analytics-sa.json.enc

GOOGLE_ADS: /app/reporting_configs/google-ads-sa.json
⊘ Not found: /app/reporting_configs/google-ads-sa.json

GA4: /app/data/google_analytics/ga_service_account.json
[DRY RUN] Would encrypt: /app/data/google_analytics/ga_service_account.json → /app/data/google_analytics/ga_service_account.json.enc

============================================================
Summary
============================================================
Successful: 2/3

[DRY RUN] No files were modified
Run without --dry-run to actually encrypt files
```

### Phase 2: Encrypt Files (Keep Plaintext)

Encrypt service accounts while keeping plaintext as backup:

```bash
sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --all
```

**Expected output**:
```
============================================================
Service Account Encryption Utility
============================================================

Encrypting all service accounts:

YOUTUBE: /app/reporting_configs/youtube-analytics-sa.json
✓ Encrypted: /app/reporting_configs/youtube-analytics-sa.json → /app/reporting_configs/youtube-analytics-sa.json.enc

GA4: /app/data/google_analytics/ga_service_account.json
✓ Encrypted: /app/data/google_analytics/ga_service_account.json → /app/data/google_analytics/ga_service_account.json.enc

============================================================
Summary
============================================================
Successful: 2/2

✓ All service accounts encrypted successfully!

Your service account keys are now encrypted at rest.
The import scripts will automatically use encrypted files.
```

### Phase 3: Test Imports

Verify imports work with encrypted files:

```bash
# Test YouTube Analytics import
sudo docker exec imports_container python3 /app/python/import.py youtube get_youtube_analytics --start_date=2026-01-28 --end_date=2026-01-29

# Test Google Analytics import
sudo docker exec imports_container python3 /app/python/import.py google_analytics run_daily_imports

# Check logs for encryption warnings
sudo docker logs imports_container --tail 50 | grep -i "encrypt\|credential"
```

**Expected log output**:
```
✓ Service account credentials loaded successfully
```

**No warnings about unencrypted files** means encrypted files are being used.

### Phase 4: Delete Plaintext (Optional)

Once you've verified imports work, optionally delete plaintext files:

```bash
# This will DELETE plaintext files after verifying encrypted versions exist
sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --all --delete-plaintext
```

**⚠️ WARNING**: This permanently deletes plaintext files. Make sure:
1. Encrypted files exist and work
2. You have backups elsewhere
3. Imports have been tested successfully

---

## Verification Commands

### Check Which Files Are Encrypted

```bash
# List all service account files (plaintext and encrypted)
sudo docker exec imports_container find /app/reporting_configs /app/data/google_analytics/credentials -name "*sa*.json*" -ls

# Check YouTube Analytics
sudo docker exec imports_container ls -la /app/reporting_configs/youtube-analytics-sa.json*

# Check Google Ads
sudo docker exec imports_container ls -la /app/reporting_configs/google-ads-sa.json*

# Check GA4
sudo docker exec imports_container ls -la /app/data/google_analytics/ga_service_account.json*
```

### Test Decryption

```bash
# Verify encryption/decryption works against any .enc file
sudo docker exec imports_container python3 -c "
import sys
sys.path.insert(0, '/app/python')
from lib.encryption import decrypt_json_file
data = decrypt_json_file('/app/reporting_configs/youtube-oauth-client.json.enc')
assert data, 'decryption returned empty'  # -> raises on failure
"
```

---

## Troubleshooting

### Error: "Encryption key (enc_key) not found in config file"

**Cause**: Master config file missing encryption key.

**Solution**:
```bash
# Check if encryption key exists
sudo docker exec imports_container python3 -c "
import yaml
with open('/app/reporting_configs/master-config.yaml') as f:
    config = yaml.safe_load(f)
    print('Encryption key exists:', 'enc_key' in config)
"

# Verify environment variable
sudo docker exec imports_container env | grep ENC_KEY
```

The encryption key should be automatically generated during container setup. If missing, regenerate it:
```bash
# Regenerate encryption key (will invalidate existing encrypted files!)
sudo docker stop imports_container
sudo docker rm imports_container
# Re-run: bin/setup.sh → Option 9 (Install Imports Container)
```

---

### Error: "InvalidToken" when decrypting

**Cause**: File was encrypted with different key, or file is corrupted.

**Solution**:
1. **Re-encrypt the file**:
   ```bash
   # Delete corrupted .enc file
   sudo docker exec imports_container rm /app/reporting_configs/youtube-analytics-sa.json.enc

   # Re-encrypt from plaintext
   sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --file /app/reporting_configs/youtube-analytics-sa.json
   ```

2. **If plaintext was deleted**, restore from backup:
   ```bash
   # Copy backup to server
   scp -i ~/.ssh/id_ed25519 youtube-analytics-sa.json ubuntu@<server-ip>:/tmp/

   # Move to container
   sudo docker exec imports_container bash -c 'mv /tmp/youtube-analytics-sa.json /app/reporting_configs/ && chmod 600 /app/reporting_configs/youtube-analytics-sa.json'

   # Re-encrypt
   sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --file /app/reporting_configs/youtube-analytics-sa.json
   ```

---

### Warning: "Loading UNENCRYPTED service account"

**Symptom**: Import works but logs show warning about unencrypted file.

**Solution**: This is expected if `.enc` file doesn't exist yet. Encrypt the file:
```bash
sudo docker exec imports_container python3 /app/python/import_scripts/encrypt_service_accounts.py --file /app/reporting_configs/youtube-analytics-sa.json
```

After encryption, re-run import and warning should disappear.

---

### File Permissions After Encryption

Encrypted files should have **600 permissions** (owner read/write only):

```bash
# Check permissions
sudo docker exec imports_container ls -la /app/reporting_configs/*.enc

# Fix if needed
sudo docker exec imports_container chmod 600 /app/reporting_configs/*.enc
```

Expected output:
```
-rw------- 1 root root 2445 Jan 29 12:00 youtube-analytics-sa.json.enc
```

---

## Manual Encryption (Python API)

If you prefer to encrypt files programmatically:

```python
import sys
sys.path.insert(0, '/app/python')
from lib.encryption import encrypt_json_file, decrypt_json_file

# Encrypt a JSON file
encrypt_json_file(
    '/app/reporting_configs/youtube-oauth-client.json',
    '/app/reporting_configs/youtube-oauth-client.json.enc'
)

# Decrypt and load as dictionary
creds = decrypt_json_file('/app/reporting_configs/youtube-oauth-client.json.enc')
```

---

## Security Best Practices

### ✅ Do

1. **Encrypt all service accounts** in production
2. **Keep 600 file permissions** on both plaintext and encrypted files
3. **Test imports after encryption** before deleting plaintext
4. **Backup plaintext keys** in secure password manager before deletion
5. **Rotate encryption key** if compromised (requires re-encrypting all files)

### ❌ Don't

1. **Don't commit plaintext keys** to version control
2. **Don't share encrypted files publicly** (still sensitive if encryption key leaks)
3. **Don't delete plaintext** without verifying encrypted files work
4. **Don't change encryption key** without re-encrypting all files
5. **Don't use same service account** across environments (dev/staging/prod)

---

## Key Rotation

Rotation is a **three-state process**. The app decrypts with the
primary key and then any previous keys (`lib/encryption.py` uses cryptography's
`MultiFernet`), so reads never break while both keys are held; new writes always
use the primary. Nothing is edited on the host or in master-config.yaml — the
keys live in AWS SSM and reach the container through
`deployment/start-secrets-container.sh`.

| Env var (container) | SSM param | Meaning |
|---|---|---|
| `ENC_KEY` | `/km/<env>/deam/enc-key` | **Primary.** Every new ciphertext. Required. |
| `ENC_KEY_PREVIOUS` | `/km/<env>/deam/enc-key-previous` | Retiring key(s), comma-separated, newest first. Optional; present only during a rotation window. |

`ENC_KEY_PREVIOUS` is read **only from the environment** — never from the YAML —
so a rotation cannot leave a second live key on disk.

### Step 0: Precondition and baseline

The host must already be SSM-sourced (Phase 3: `ENC_KEY` is in the
container env and `enc_key` is stripped from master-config.yaml). Check, and
record the pre-rotation counts:

```bash
docker exec -w /app/python imports_container python3 -m tools.rotate_enc_key --status
# expect: primary key source : env ; previous keys held : 0 ; master-config enc_key : absent
# and a per-index table with stale=0 (everything is under the current key)
```

If `master-config.yaml still carries enc_key` is printed, finish the
strip first — rotating while a YAML copy exists just leaves the old key behind.

Take an ES snapshot (or confirm today's `daily-snapshots` run) **before** step 1:
a pre-rotation snapshot holds ciphertexts under the old key, and restoring it
later needs that key from SSM's parameter history (see "Recovery").

### Step 1: Generate the new key (never print it to a shared terminal)

```bash
NEW_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

### Step 2: Stage both keys in SSM (km-infra / SSM writer identity)

Move the current primary into `enc-key-previous`, then make the new key the
primary. Both are `SecureString` under the same KMS key as `enc-key`.

```bash
ENV=dev   # then prod, after dev verifies
OLD_KEY=$(aws ssm get-parameter --name /km/$ENV/deam/enc-key --with-decryption --query Parameter.Value --output text)
aws ssm put-parameter --name /km/$ENV/deam/enc-key-previous --type SecureString --value "$OLD_KEY" --overwrite
aws ssm put-parameter --name /km/$ENV/deam/enc-key --type SecureString --value "$NEW_KEY" --overwrite
unset OLD_KEY NEW_KEY
```

### Step 3: Recreate the container with both keys

The wrapper takes **no arguments**. The environment is chosen by `SSM_ENV`
(default `dev`), the reader identity by `SSM_PROFILE` / `SSM_SUDO` — exactly the
forms documented in the wrapper's header. Getting this wrong on prod would fetch
`/km/dev/*` and write dev's `ENC_KEY` into prod's `.env`, orphaning every stored
token in one step, so copy the line for the box you are on:

```bash
# on the DEAM host, as ubuntu (no outer sudo)
# dev:
bash deployment/start-secrets-container.sh
# staging (repo lives elsewhere):
REPO_DIR=$HOME/DE-Reporting bash deployment/start-secrets-container.sh
# prod:
SSM_ENV=prod SSM_PROFILE="" SSM_SUDO=1 bash deployment/start-secrets-container.sh

docker exec imports_container printenv ENC_KEY_PREVIOUS | wc -c    # > 1 means the previous key is present
```

From here every read still works and every new write is already under the new
key. Do not stop here for long: the window is where two keys are live.

### Step 4: Re-encrypt everything that is still under the old key

```bash
docker exec -w /app/python imports_container python3 -m tools.rotate_enc_key            # dry-run: lists what would rotate
docker exec -w /app/python imports_container python3 -m tools.rotate_enc_key --apply    # rewrites stale docs + *.enc files
```

The tool scans every index in its `INVENTORY` (connections, FB apps / app users,
Echo tenants, Chatwoot accounts, YouTube / GA4 accounts, MFA secrets, pending
registrations) by **shape**, not by field name, so an un-listed field is rotated
too and reported as `uninventoried` — add it to `INVENTORY` when that happens.
`undecryptable` values decrypt under no held key (pre-existing orphans); they
are left alone and counted. A `conflict` is a document that changed mid-run;
re-run `--apply` until the summary ends with `stale remaining=0` (exit 0). Exit
2 means something is still stale.

To widen the scan beyond the inventory: `--index-pattern 'de_asset_*'`.

### Step 5: Retire the previous key

```bash
aws ssm delete-parameter --name /km/$ENV/deam/enc-key-previous
# re-run the wrapper with the SAME environment form as step 3 (no arguments; SSM_ENV picks the env)
bash deployment/start-secrets-container.sh                                            # dev
SSM_ENV=prod SSM_PROFILE="" SSM_SUDO=1 bash deployment/start-secrets-container.sh     # prod
# either form writes ENC_KEY_PREVIOUS= blank and recreates the container
docker exec imports_container printenv ENC_KEY_PREVIOUS | wc -c    # 1 (empty line) — previous key gone
docker exec -w /app/python imports_container python3 -m tools.rotate_enc_key --status   # previous keys held : 0 ; stale=0 ; undecryptable unchanged
```

The wrapper writes rotation-window variables **blank** when their SSM param is
absent (`CLEAR_IF_ABSENT`), so deleting the param is what removes the old key
from `deployment/.env` and the container.

### Step 6: Verify a real read

Open a connector that imports today (Data Connectors → a GA4 or Google Ads
card) and confirm the next import log shows no `decrypt` / `InvalidToken`
error; on the Security page confirm MFA login still works for one TOTP user.

### Recovery

* **A restore of a pre-rotation ES snapshot** carries ciphertexts under the old
  key. Put the old value back into `enc-key-previous` from SSM's parameter
  history (`aws ssm get-parameter-history --name /km/$ENV/deam/enc-key
  --with-decryption`, the version before the rotation), re-run the wrapper, run
  the tool with `--apply`, then retire it again (steps 3–5).
* **Rolling back the rotation itself** (before step 5): swap the two SSM values
  back and re-run the wrapper; the tool's `--apply` then rotates everything onto
  the restored primary.
* **A key lost ** shows up as `undecryptable`; nothing can
  recover those values — the user re-authorizes the connector / re-enrols MFA.

---

## Supported Service Accounts

| Service Account | Path | Supported Since |
|-----------------|------|-----------------|
| **YouTube Analytics** | `/app/reporting_configs/youtube-analytics-sa.json` | v1.0.0 |
| **Google Ads** | `/app/reporting_configs/google-ads-sa.json` | v1.0.0 |
| **Google Analytics (GA4)** | `/app/data/google_analytics/ga_service_account.json` | v1.0.0 |

**All import scripts now auto-detect encrypted files**. No configuration changes needed - just encrypt the files and imports will automatically use the `.enc` versions.

---

## FAQ

### Do I need to encrypt service accounts?

**Not required**, but **strongly recommended** for:
- Production environments
- Servers accessible by multiple users
- Compliance requirements (SOC 2, ISO 27001)
- Enhanced security posture

### Will encryption slow down imports?

**No**. Decryption overhead is negligible:
- Fernet decryption: <1ms per file
- Files are decrypted once at import start
- No performance impact on data fetching

### Can I use different encryption keys per service account?

**No**. Currently all encrypted files use the same key from `master-config.yaml`. This simplifies key management.

### What happens if I lose the encryption key?

**Encrypted files become unrecoverable**. Always:
1. Backup plaintext files before deletion
2. Store encryption key in secure password manager
3. Document key rotation procedures

---

**Last Updated**: 2026-01-29
**Version**: 1.0.0
**Author**: Digital Engagement Dashboard Team
