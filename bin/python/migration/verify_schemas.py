#!/usr/bin/env python3
"""
Verify migration schema files are valid JSON and have expected field counts.
"""

import json
import os


def verify_schema(schema_file, expected_count):
    """Verify a schema file and return results."""
    schema_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "schemas", schema_file)

    try:
        with open(schema_path, "r") as f:
            data = json.load(f)

        if "properties" not in data:
            return False, "Missing 'properties' key"

        field_count = len(data["properties"])

        if field_count == expected_count:
            return True, f"{field_count} fields (PASS)"
        else:
            return False, f"{field_count} fields (expected {expected_count}) (FAIL)"

    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"
    except FileNotFoundError:
        return False, "File not found"
    except Exception as e:
        return False, f"Error: {e}"


def main():
    schemas = {
        "unified_schema_fb_insights.json": 89,
        "unified_schema_ga_metrics.json": 24,
        "unified_schema_echo_conversations.json": 32,
        "unified_schema_ig_insights.json": 27,
    }

    print("=" * 60)
    print("Schema Verification Report")
    print("=" * 60)

    all_passed = True

    for schema_file, expected_count in schemas.items():
        passed, message = verify_schema(schema_file, expected_count)
        status = "[PASS]" if passed else "[FAIL]"
        print(f"\n{schema_file}:")
        print(f"  Status: {status}")
        print(f"  Details: {message}")

        if not passed:
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("All schemas validated successfully!")
    else:
        print("Some schemas failed validation!")
    print("=" * 60)

    return 0 if all_passed else 1


if __name__ == "__main__":
    exit(main())
