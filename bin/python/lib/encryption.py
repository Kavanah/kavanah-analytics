from cryptography.fernet import Fernet, InvalidToken, MultiFernet
import re
import yaml
import os
import json
import logging
from datetime import datetime

# key rotation. ``ENC_KEY`` is the PRIMARY key: every new ciphertext
# is written under it. ``ENC_KEY_PREVIOUS`` (optional, comma-separated, newest
# first) lists keys that may still decrypt existing ciphertexts during a
# rotation window. Encrypt uses the primary only; decrypt tries the primary
# then each previous key (cryptography's MultiFernet). Previous keys come ONLY
# from the environment — never from master-config.yaml — because rotation only
# makes sense on an SSM-sourced host and a YAML fallback would put a
# second live key back on disk, which is the thing rotation is meant to retire.
PRIMARY_KEY_ENV = "ENC_KEY"
PREVIOUS_KEYS_ENV = "ENC_KEY_PREVIOUS"

# Shape of a Fernet token: version byte 0x80 then a 64-bit big-endian timestamp
# whose top bytes are zero for any date before year ~36,000, so every token
# base64-encodes to a string starting "gAAAAA". Minimum length is 1+8+16+16+32
# raw bytes (an empty plaintext still pads to one AES block) = 73 bytes → 100
# base64 characters. Used by the rotation tool to FIND encrypted values in a
# document without an inventory of every field name.
_FERNET_TOKEN_RE = re.compile(r"^gAAAAA[A-Za-z0-9_-]{92,}={0,2}$")


def load_encryption_key():
    """Load the Fernet encryption key, env var first then master-config.yaml.

    Precedence: the ``ENC_KEY`` environment variable wins when set, so
    the host can inject it from AWS SSM (the host-fetch → env-file →
    compose pattern) without the secret living in the YAML. When the env var is
    absent (or empty) it falls back to the ``enc_key`` value in
    master-config.yaml — byte-for-byte the legacy behavior. If neither source
    provides a key, raise: ``enc_key`` decrypts every stored connector token, so
    a silent default would mass-corrupt reads and must never happen.
    """
    env_key = os.environ.get("ENC_KEY")
    if env_key:
        return env_key

    config_path = "/app/reporting_configs/master-config.yaml"  # Adjust the path as needed
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found at {config_path}")

    with open(config_path, "r") as config_file:
        config = yaml.safe_load(config_file)
        key = config.get("enc_key")
        if key is None:
            raise ValueError("Encryption key not found: set the ENC_KEY env var or enc_key in the config file.")
        # Return the base64-encoded key as is, no need for further encoding
        return key


def key_source():
    """Where the PRIMARY key would be read from right now: ``"env"`` or ``"yaml"``.

    Reports the source without returning the key, so status output and logs can
    say "env-sourced" or "still on disk" and never the value. Raises
    the same errors as :func:`load_encryption_key` when no key is available.
    """
    if os.environ.get(PRIMARY_KEY_ENV):
        return "env"
    load_encryption_key()  # raises if the YAML has no key either
    return "yaml"


def load_previous_keys():
    """Return the previous keys from ``ENC_KEY_PREVIOUS`` (newest first).

    Comma-separated; blank entries and surrounding whitespace are ignored, so an
    unset or empty variable means "no previous keys" (the steady state between
    rotations). Never falls back to master-config.yaml — see the module note.

    Each entry is validated by constructing a ``Fernet`` from it; a malformed
    entry (a typo in the SSM param) is logged by POSITION — never by value — and
    skipped, so one bad previous key can only fail to rotate its own tokens, not
    take every decrypt in the app down with it (review).
    """
    raw = os.environ.get(PREVIOUS_KEYS_ENV, "") or ""
    keys = []
    for position, candidate in enumerate((k.strip() for k in raw.split(",")), start=1):
        if not candidate:
            continue
        try:
            Fernet(candidate)
        except (ValueError, TypeError):
            logging.warning(
                "%s entry %d is not a valid Fernet key and was skipped; the primary key is unaffected",
                PREVIOUS_KEYS_ENV,
                position,
            )
            continue
        keys.append(candidate)
    return keys


def _primary_fernet():
    return Fernet(load_encryption_key())


def _multi_fernet():
    """Primary first, then previous keys — the order MultiFernet encrypts/rotates to."""
    return MultiFernet([_primary_fernet()] + [Fernet(k) for k in load_previous_keys()])


def key_ring():
    """``(primary_fernet, multi_fernet)`` built ONCE for a long-running caller.

    ``encrypt``/``decrypt`` re-read the environment on every call on purpose (a
    recreated container is the unit of key change, so per-call cost is the price
    of never caching a key in process memory longer than a request). A batch
    caller such as ``tools.rotate_enc_key`` classifies thousands of tokens in one
    run and should build the objects once — this is that entry point.
    """
    return _primary_fernet(), _multi_fernet()


def encrypt(plaintext):
    """Encrypt the given plaintext under the PRIMARY key (``ENC_KEY``)."""
    return _primary_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext):
    """Decrypt the given ciphertext with the primary key, then any previous keys.

    A ciphertext written under a retired key still decrypts while that key is
    listed in ``ENC_KEY_PREVIOUS``; once the list is emptied it raises
    ``InvalidToken`` like any foreign token. Callers see no difference during a
    rotation — that is the point.
    """
    return _multi_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def is_current(ciphertext):
    """True when ``ciphertext`` decrypts under the PRIMARY key alone.

    False means it was written under some other key — either a previous key
    (rotate it) or none we hold (undecryptable). Distinguish the two with
    :func:`decrypt`.
    """
    try:
        _primary_fernet().decrypt(ciphertext.encode("utf-8"))
    except InvalidToken:
        return False
    return True


def rotate(ciphertext):
    """Re-encrypt ``ciphertext`` under the PRIMARY key, keeping its timestamp.

    Decrypts with the primary or any previous key and re-encrypts with the
    primary (``MultiFernet.rotate``). Raises ``InvalidToken`` when no held key
    decrypts it — the caller must leave such values alone and report them.
    """
    return _multi_fernet().rotate(ciphertext.encode("utf-8")).decode("utf-8")


def rotate_bytes(token):
    """Bytes-in/bytes-out variant of :func:`rotate` for ``.enc`` files."""
    return _multi_fernet().rotate(token)


def looks_like_fernet_token(value):
    """True when ``value`` is a string with the shape of a Fernet token.

    Shape only — no key is consulted. A false positive would have to be a
    base64url string of 100+ characters starting ``gAAAAA``; nothing else in
    the app's documents has that form.
    """
    return isinstance(value, str) and bool(_FERNET_TOKEN_RE.match(value))


def encrypt_json_file(input_path, output_path=None):
    """
    Encrypt a JSON file and save as .enc file.

    Args:
        input_path: Path to plaintext JSON file
        output_path: Path for encrypted output (default: input_path + '.enc')

    Returns:
        str: Path to encrypted file

    Raises:
        FileNotFoundError: If input file doesn't exist
        json.JSONDecodeError: If input is not valid JSON

    Example:
        encrypt_json_file('/app/reporting_configs/youtube-analytics-sa.json')
        # Creates: /app/reporting_configs/youtube-analytics-sa.json.enc
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if output_path is None:
        output_path = input_path + ".enc"

    # Read and validate JSON
    with open(input_path, "r") as f:
        json_data = json.load(f)  # Validates JSON format
        plaintext = json.dumps(json_data)

    # Encrypt under the primary key
    encrypted = _primary_fernet().encrypt(plaintext.encode("utf-8"))

    # Write encrypted file
    with open(output_path, "wb") as f:
        f.write(encrypted)

    logging.info(f"Encrypted {input_path} → {output_path}")
    return output_path


def decrypt_json_file(encrypted_path):
    """
    Decrypt an encrypted JSON file and return as dict.

    Args:
        encrypted_path: Path to encrypted .enc file

    Returns:
        dict: Decrypted JSON data as dictionary

    Raises:
        FileNotFoundError: If encrypted file doesn't exist
        json.JSONDecodeError: If decrypted data is not valid JSON
        cryptography.fernet.InvalidToken: If decryption fails

    Example:
        data = decrypt_json_file('/app/reporting_configs/youtube-analytics-sa.json.enc')
        print(data['client_email'])
    """
    if not os.path.exists(encrypted_path):
        raise FileNotFoundError(f"Encrypted file not found: {encrypted_path}")

    # Read encrypted file
    with open(encrypted_path, "rb") as f:
        encrypted = f.read()

    # Decrypt with the primary key, then any previous keys (rotation window)
    plaintext = _multi_fernet().decrypt(encrypted).decode("utf-8")

    # Parse JSON
    json_data = json.loads(plaintext)

    return json_data
