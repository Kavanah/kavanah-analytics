from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
)

INDEX_NAME = "de_asset_fb_accounts"


def add():
    # Define form details
    title = "Adding New Facebook Account"
    instructions = [
        "Please provide the following details for the new Facebook Account.",
        "Fields marked with * are required.",
    ]
    fields = [
        {
            "name": "fb_account_id",
            "prompt": "Enter Facebook Account ID*:",
            "type": "text",
        },
        {"name": "fb_account_title", "prompt": "Enter Account Title*:", "type": "text"},
        {
            "name": "fb_account_description",
            "prompt": "Enter Description:",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # Index the data into Elasticsearch using the helper function
        if add_document(INDEX_NAME, responses):
            print("Facebook Account added successfully!")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Facebook Accounts found.")
        return

    # List titles and allow user to select one for details
    print("Facebook Accounts:")
    accounts = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("fb_account_title", "No Title")
        account_id = hit["_id"]  # Use Elasticsearch's _id for retrieving individual documents
        accounts.append((account_id, title))
        print(f"{idx}. {title}")

    # Ensure valid input selection
    while True:
        try:
            selected_idx = int(input("\nEnter the number of the Facebook Account to view: ")) - 1
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

    print("\nFacebook Account Details:")
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
    title = "Editing Facebook Account"
    instructions = ["Please review and edit the fields as needed. You can select a specific field to edit."]
    fields = [
        {
            "name": "fb_account_id",
            "prompt": f"Facebook Account ID [{account['fb_account_id']}]:",
            "type": "text",
        },
        {
            "name": "fb_account_title",
            "prompt": f"Account Title [{account['fb_account_title']}]:",
            "type": "text",
        },
        {
            "name": "fb_account_description",
            "prompt": f"Description [{account.get('fb_account_description', '')}]:",
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
            print("Facebook Account updated successfully!")
    elif action == "cancel":
        print("Operation canceled.")


def delete(account_id):
    # Confirm deletion
    confirmation = input(f"Are you sure you want to delete the Facebook Account? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, account_id):
            print("Facebook Account deleted successfully.")
    else:
        print("Delete operation canceled.")
