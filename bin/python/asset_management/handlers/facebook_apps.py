from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
)
from lib.encryption import decrypt

INDEX_NAME = "de_asset_fb_apps"


def add():
    # Define form details
    title = "Adding New Facebook App"
    instructions = [
        "Please provide the following details for the new Facebook App.",
        "Fields marked with * are required.",
    ]
    fields = [
        {"name": "fb_app_id", "prompt": "Enter Facebook App ID*:", "type": "text"},
        {
            "name": "fb_app_secret",
            "prompt": "Enter App Secret*:",
            "type": "text",
            "encrypted": True,
        },
        {
            "name": "fb_app_token",
            "prompt": "Enter App Token*:",
            "type": "text",
            "encrypted": True,
        },
        {"name": "fb_app_title", "prompt": "Enter App Title*:", "type": "text"},
        {"name": "fb_app_description", "prompt": "Enter Description:", "type": "text"},
        {
            "name": "fb_account_title",
            "prompt": "Select Facebook Account*:",
            "type": "choice",
            "es_index": "de_asset_fb_accounts",
            "title_field": "fb_account_title",
            "module": "facebook_accounts",
            "store_associated_id": True,
        },
        # Hidden field for the selected fb_account_esid
        {"name": "fb_account_esid", "type": "hidden"},
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # Index the data into Elasticsearch using the helper function
        if add_document(INDEX_NAME, responses):
            print("Facebook App added successfully!")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Facebook Apps found.")
        return

    # List titles and allow user to select one for details
    print("Facebook Apps:")
    apps = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("fb_app_title", "No Title")
        app_id = hit["_id"]  # Use Elasticsearch's _id for retrieving individual documents
        apps.append((app_id, title))
        print(f"{idx}. {title}")

    # Ensure valid input selection
    while True:
        try:
            selected_idx = int(input("\nEnter the number of the Facebook App to view: ")) - 1
            if 0 <= selected_idx < len(apps):
                selected_id, selected_title = apps[selected_idx]
                break  # Exit the loop if the input is valid
            else:
                print(f"Invalid selection. Please enter a number between 1 and {len(apps)}.")
        except ValueError:
            print("Invalid input. Please enter a valid number.")

    # Pass the Elasticsearch-generated _id to view_details
    view_details(selected_id)


def view_details(app_id):
    app = get_document_by_id(INDEX_NAME, app_id)
    if not app:
        return

    print("\nFacebook App Details:")
    print("----------------------")
    for key, value in app.items():
        if key in ["fb_app_secret", "fb_app_token"]:  # Decrypt sensitive fields
            try:
                value = decrypt(value)
            except Exception as e:
                print(f"Error decrypting {key}: {e}")
        print(f"{key}: {value}")
    print()

    # Options: Edit, Delete, or Back
    print("Options:")
    print("1) Edit")
    print("2) Delete")
    print("3) Back")
    choice = input("Enter your choice: ").strip()

    if choice == "1":
        edit(app_id)
    elif choice == "2":
        delete(app_id)
    elif choice == "3":
        return
    else:
        print("Invalid choice. Returning to main menu.")


def edit(app_id):
    app = get_document_by_id(INDEX_NAME, app_id)
    if not app:
        return

    # Define the form with pre-filled values
    title = "Editing Facebook App"
    instructions = ["Please review and edit the fields as needed. You can select a specific field to edit."]
    fields = [
        {
            "name": "fb_app_id",
            "prompt": f"Facebook App ID [{app['fb_app_id']}]:",
            "type": "text",
        },
        {
            "name": "fb_app_secret",
            "prompt": "App Secret:",
            "type": "text",
            "encrypted": True,
        },
        {
            "name": "fb_app_token",
            "prompt": "App Token:",
            "type": "text",
            "encrypted": True,
        },
        {
            "name": "fb_app_title",
            "prompt": f"App Title [{app['fb_app_title']}]:",
            "type": "text",
        },
        {
            "name": "fb_app_description",
            "prompt": f"Description [{app.get('fb_app_description', '')}]:",
            "type": "text",
        },
        {
            "name": "fb_account_title",
            "prompt": f"Facebook Account [{app.get('fb_account_title', '')}]:",
            "type": "choice",
            "es_index": "de_asset_fb_accounts",
            "title_field": "fb_account_title",
            "module": "facebook_accounts",
            "store_associated_id": True,
        },
        # Hidden field for the selected fb_account_esid
        {"name": "fb_account_esid", "type": "hidden"},
    ]

    form = FormHandler(title, instructions, fields)

    # Pre-fill the form responses with the current data
    form.responses = {key: app.get(key, "") for key in app.keys()}

    # Use the review flow to allow the user to select which fields to edit
    action, responses = form.edit_mode()

    if action == "submit":
        # Update only the fields that were changed
        updated_document = {**app, **responses}
        if edit_document(INDEX_NAME, app_id, updated_document):
            print("Facebook App updated successfully!")
    elif action == "cancel":
        print("Operation canceled.")


def delete(app_id):
    # Confirm deletion
    confirmation = input(f"Are you sure you want to delete the Facebook App? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, app_id):
            print("Facebook App deleted successfully.")
    else:
        print("Delete operation canceled.")
