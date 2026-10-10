from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
    get_elasticsearch_client,
)
from datetime import datetime

INDEX_NAME = "de_asset_ga_accounts"


def add():
    title = "Adding New Google Analytics Account"
    instructions = [
        "Please provide the following details for the new Google Analytics Account.",
        "Fields marked with * are required.",
        "Note: Service account credentials are configured in /app/reporting_configs/google-analytics.yaml",
    ]
    fields = [
        {"name": "ga_account_title", "prompt": "Enter Account Title*:", "type": "text"},
        {
            "name": "ga_account_id",
            "prompt": "Enter Account ID (e.g., 123456789)*:",
            "type": "text",
        },
        {
            "name": "ga_account_resource_name",
            "prompt": "Enter Resource Name (e.g., accounts/123456789):",
            "type": "text",
        },
        {
            "name": "ga_account_description",
            "prompt": "Enter Description (optional):",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # Auto-generate resource name if not provided
        if not responses.get("ga_account_resource_name") and responses.get("ga_account_id"):
            responses["ga_account_resource_name"] = f"accounts/{responses['ga_account_id']}"

        # Add timestamp field required by Elasticsearch
        responses["@timestamp"] = datetime.utcnow().isoformat()

        # Index the data into Elasticsearch
        if add_document(INDEX_NAME, responses):
            print("Google Analytics Account added successfully!")
        else:
            print("Failed to add Google Analytics Account.")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Google Analytics Accounts found.")
        return

    # List titles and allow user to select one for details
    print("Google Analytics Accounts:")
    accounts = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("ga_account_title", "No Title")
        account_id = hit["_id"]
        accounts.append((account_id, title))
        print(f"{idx}. {title}")

    while True:
        try:
            choice = input("\nEnter number to view details, 'e' to edit, 'd' to delete, or 'b' to go back: ").strip().lower()
            if choice == "b":
                return
            elif choice == "e":
                edit_choice = int(input("Enter number to edit: ").strip())
                if 1 <= edit_choice <= len(accounts):
                    account_id, _ = accounts[edit_choice - 1]
                    edit_account(account_id)
                    return
            elif choice == "d":
                delete_choice = int(input("Enter number to delete: ").strip())
                if 1 <= delete_choice <= len(accounts):
                    account_id, title = accounts[delete_choice - 1]
                    confirm = input(f"Are you sure you want to delete '{title}'? (yes/no): ").strip().lower()
                    if confirm == "yes":
                        if delete_asset_with_cleanup(INDEX_NAME, account_id):
                            print(f"Deleted '{title}' successfully.")
                        else:
                            print(f"Failed to delete '{title}'.")
                    return
            else:
                choice_int = int(choice)
                if 1 <= choice_int <= len(accounts):
                    account_id, _ = accounts[choice_int - 1]
                    display_account_details(account_id)
                else:
                    print("Invalid choice. Please try again.")
        except ValueError:
            print("Invalid input. Please enter a number, 'e', 'd', or 'b'.")


def display_account_details(account_id):
    account = get_document_by_id(INDEX_NAME, account_id)
    if not account:
        print("Account not found.")
        return

    print("\n" + "=" * 50)
    print("Google Analytics Account Details")
    print("=" * 50)
    print(f"Account Title: {account.get('ga_account_title', 'N/A')}")
    print(f"Account ID: {account.get('ga_account_id', 'N/A')}")
    print(f"Resource Name: {account.get('ga_account_resource_name', 'N/A')}")
    print(f"Description: {account.get('ga_account_description', 'N/A')}")
    print("\nNote: Service account credentials are configured in /app/reporting_configs/google-analytics.yaml")
    print("=" * 50 + "\n")


def edit_account(account_id):
    account = get_document_by_id(INDEX_NAME, account_id)
    if not account:
        print("Account not found.")
        return

    print("\n" + "=" * 50)
    print(f"Editing: {account.get('ga_account_title', 'N/A')}")
    print("=" * 50)
    print("Leave fields blank to keep current values.\n")

    # Account Title
    new_title = input(f"Account Title [{account.get('ga_account_title', '')}]: ").strip()
    if new_title:
        account["ga_account_title"] = new_title

    # Account ID
    new_account_id = input(f"Account ID [{account.get('ga_account_id', '')}]: ").strip()
    if new_account_id:
        account["ga_account_id"] = new_account_id
        # Auto-update resource name
        account["ga_account_resource_name"] = f"accounts/{new_account_id}"

    # Resource Name
    new_resource_name = input(f"Resource Name [{account.get('ga_account_resource_name', '')}]: ").strip()
    if new_resource_name:
        account["ga_account_resource_name"] = new_resource_name

    # Description
    new_description = input(f"Description [{account.get('ga_account_description', '')}]: ").strip()
    if new_description:
        account["ga_account_description"] = new_description

    # Confirm changes
    confirm = input("\nSave changes? (yes/no): ").strip().lower()
    if confirm == "yes":
        # Update timestamp
        account["@timestamp"] = datetime.utcnow().isoformat()

        if edit_document(INDEX_NAME, account_id, account):
            print("Google Analytics Account updated successfully!")
        else:
            print("Failed to update Google Analytics Account.")
    else:
        print("Changes discarded.")
