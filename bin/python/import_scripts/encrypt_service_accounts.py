#!/usr/bin/env python3
"""
Service Account Encryption Utility

Encrypts service account JSON files to secure .enc files.
Run this script to encrypt your service account keys for enhanced security.

Usage:
    # Encrypt all service accounts
    python3 /app/python/import_scripts/encrypt_service_accounts.py --all

    # Encrypt specific service account
    python3 /app/python/import_scripts/encrypt_service_accounts.py --file /app/reporting_configs/youtube-analytics-sa.json

    # Dry run (preview what would be encrypted)
    python3 /app/python/import_scripts/encrypt_service_accounts.py --all --dry-run
"""

import os
import sys
import argparse
import logging

# Add parent directory to path for lib imports
sys.path.insert(0, "/app/python")
import lib.logging as custom_logging
from lib.encryption import encrypt_json_file

# Define service account paths.
# the Google Ads and GA4 service accounts are retired (those
# connectors are OAuth-only). YouTube is the only remaining service-account
# connector.
SERVICE_ACCOUNTS = {
    "youtube": "/app/reporting_configs/youtube-analytics-sa.json",
}


def encrypt_service_account(file_path, dry_run=False, delete_plaintext=False):
    """
    Encrypt a service account JSON file.

    Args:
        file_path: Path to service account JSON
        dry_run: If True, don't actually encrypt
        delete_plaintext: If True, delete plaintext after encryption

    Returns:
        bool: True if successful, False otherwise
    """
    encrypted_path = file_path + ".enc"

    # Check if plaintext exists
    if not os.path.exists(file_path):
        logging.warning(f"⊘ Not found: {file_path}")
        return False

    # Check if already encrypted
    if os.path.exists(encrypted_path):
        logging.info(f"✓ Already encrypted: {encrypted_path}")
        if delete_plaintext and os.path.exists(file_path):
            if dry_run:
                logging.info(f"  [DRY RUN] Would delete plaintext: {file_path}")
            else:
                os.unlink(file_path)
                logging.info(f"  Deleted plaintext: {file_path}")
        return True

    if dry_run:
        logging.info(f"[DRY RUN] Would encrypt: {file_path} → {encrypted_path}")
        return True

    # Encrypt the file
    try:
        encrypt_json_file(file_path, encrypted_path)
        logging.info(f"✓ Encrypted: {file_path} → {encrypted_path}")

        # Optionally delete plaintext
        if delete_plaintext:
            os.unlink(file_path)
            logging.info(f"  Deleted plaintext: {file_path}")

        return True

    except Exception as e:
        logging.error(f"✗ Failed to encrypt {file_path}: {e}")
        return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Encrypt service account JSON files")
    parser.add_argument("--all", action="store_true", help="Encrypt all service accounts")
    parser.add_argument("--file", help="Encrypt specific file path")
    parser.add_argument("--dry-run", action="store_true", help="Preview without encrypting")
    parser.add_argument(
        "--delete-plaintext",
        action="store_true",
        help="Delete plaintext files after encryption (use with caution!)",
    )

    args = parser.parse_args()

    # Setup logging
    custom_logging.define("encrypt-service-accounts")

    logging.info("=" * 60)
    logging.info("Service Account Encryption Utility")
    logging.info("=" * 60)

    if args.dry_run:
        logging.warning("[DRY RUN MODE] No files will be modified")

    if args.delete_plaintext and not args.dry_run:
        logging.warning("⚠️  --delete-plaintext enabled: Plaintext files will be DELETED after encryption")
        response = input("Are you sure? Type 'yes' to confirm: ")
        if response.lower() != "yes":
            logging.info("Aborted by user")
            return

    success_count = 0
    total_count = 0

    if args.all:
        logging.info("\nEncrypting all service accounts:")
        for name, path in SERVICE_ACCOUNTS.items():
            logging.info(f"\n{name.upper()}: {path}")
            total_count += 1
            if encrypt_service_account(path, args.dry_run, args.delete_plaintext):
                success_count += 1

    elif args.file:
        logging.info(f"\nEncrypting: {args.file}")
        total_count = 1
        if encrypt_service_account(args.file, args.dry_run, args.delete_plaintext):
            success_count = 1

    else:
        parser.print_help()
        return

    # Summary
    logging.info("\n" + "=" * 60)
    logging.info("Summary")
    logging.info("=" * 60)
    logging.info(f"Successful: {success_count}/{total_count}")

    if args.dry_run:
        logging.info("\n[DRY RUN] No files were modified")
        logging.info("Run without --dry-run to actually encrypt files")

    if success_count == total_count and not args.dry_run:
        logging.info("\n✓ All service accounts encrypted successfully!")
        logging.info("\nYour service account keys are now encrypted at rest.")
        logging.info("The import scripts will automatically use encrypted files.")
    elif not args.dry_run:
        logging.warning(f"\n⚠️  Some encryptions failed ({total_count - success_count} failures)")


if __name__ == "__main__":
    main()
