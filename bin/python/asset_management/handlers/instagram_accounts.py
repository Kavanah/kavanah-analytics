from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
)

INDEX_NAME = "de_asset_ig_accounts"


def add():
    # Define form details
    title = "Adding New Instagram Account"
    instructions = [
        "Please provide the following details for the new Instagram Account.",
        "Fields marked with * are required.",
    ]
    fields = [
        {"name": "ig_account_title", "prompt": "Enter Account Title*:", "type": "text"},
        {
            "name": "ig_account_id",
            "prompt": "Enter Instagram Account ID*:",
            "type": "text",
        },
        {
            "name": "ig_account_username",
            "prompt": "Enter Instagram Username*:",
            "type": "text",
        },
        {
            "name": "fb_page_id",
            "prompt": "Select Facebook Page*:",
            "type": "choice",
            "es_index": "de_asset_fb_pages",
            "title_field": "fb_page_id",
            "module": "facebook_pages",
            "store_associated_id": True,
        },
        {"name": "fb_page_esid", "type": "hidden"},
        {
            "name": "ig_account_status",
            "prompt": "Account Status:",
            "type": "choice",
            "choices": ["Active", "Disabled"],
            "allow_add": False,
        },
        {"name": "ig_account_notes", "prompt": "Notes (optional):", "type": "text"},
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # Index the data into Elasticsearch using the helper function
        if add_document(INDEX_NAME, responses):
            print("Instagram Account added successfully!")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Instagram Accounts found.")
        return

    # List accounts and allow user to select one for details
    print("Instagram Accounts:")
    accounts = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("ig_account_title", "No Title")
        username = source.get("ig_account_username", "Unknown")
        account_id = hit["_id"]  # Use Elasticsearch's _id for retrieving individual documents
        accounts.append((account_id, title))
        print(f"{idx}. {title} (@{username})")

    # Ensure valid input selection
    while True:
        try:
            selected_idx = int(input("\nEnter the number of the Instagram Account to view: ")) - 1
            if 0 <= selected_idx < len(accounts):
                selected_id, selected_title = accounts[selected_idx]
                break  # Exit the loop if the input is valid
            else:
                print(f"Invalid selection. Please enter a number between 1 and {len(accounts)}.")
        except ValueError:
            print("Invalid input. Please enter a valid number.")

    # Pass the Elasticsearch-generated _id to view_details
    view_details(selected_id)


def view_details(account_id):
    account = get_document_by_id(INDEX_NAME, account_id)
    if not account:
        return

    print("\nInstagram Account Details:")
    print("----------------------")
    for key, value in account.items():
        print(f"{key}: {value}")
    print()

    # Options: Edit, Delete, or Back
    print("Options:")
    print("1) Edit")
    print("2) Delete")
    print("3) Back")
    choice = input("Enter your choice: ").strip()

    if choice == "1":
        edit(account_id)
    elif choice == "2":
        delete(account_id)
    elif choice == "3":
        return
    else:
        print("Invalid choice. Returning to main menu.")


def edit(account_id):
    account = get_document_by_id(INDEX_NAME, account_id)
    if not account:
        return

    # Define the form with pre-filled values
    title = "Editing Instagram Account"
    instructions = ["Please review and edit the fields as needed. You can select a specific field to edit."]
    fields = [
        {
            "name": "ig_account_title",
            "prompt": f"Account Title [{account.get('ig_account_title', '')}]:",
            "type": "text",
        },
        {
            "name": "ig_account_id",
            "prompt": f"Instagram Account ID [{account.get('ig_account_id', '')}]:",
            "type": "text",
        },
        {
            "name": "ig_account_username",
            "prompt": f"Instagram Username [{account.get('ig_account_username', '')}]:",
            "type": "text",
        },
        {
            "name": "fb_page_id",
            "prompt": f"Facebook Page [{account.get('fb_page_id', '')}]:",
            "type": "choice",
            "es_index": "de_asset_fb_pages",
            "title_field": "fb_page_id",
            "module": "facebook_pages",
            "store_associated_id": True,
        },
        {"name": "fb_page_esid", "type": "hidden"},
        {
            "name": "ig_account_status",
            "prompt": f"Account Status [{account.get('ig_account_status', 'Active')}]:",
            "type": "choice",
            "choices": ["Active", "Disabled"],
            "allow_add": False,
        },
        {
            "name": "ig_account_notes",
            "prompt": f"Notes [{account.get('ig_account_notes', '')}]:",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)

    # Pre-fill the form responses with the current data
    form.responses = {key: account.get(key, "") for key in account.keys()}

    # Use the review flow to allow the user to select which fields to edit
    action, responses = form.edit_mode()

    if action == "submit":
        # Update only the fields that were changed
        updated_document = {**account, **responses}
        if edit_document(INDEX_NAME, account_id, updated_document):
            print("Instagram Account updated successfully!")
    elif action == "cancel":
        print("Operation canceled.")


def delete(account_id):
    # Confirm deletion
    confirmation = input(f"Are you sure you want to delete the Instagram Account? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, account_id):
            print("Instagram Account deleted successfully.")
    else:
        print("Delete operation canceled.")
