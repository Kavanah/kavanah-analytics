from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
)
from import_scripts.facebook.lib.fb_helpers import strip_ad_account_prefix

INDEX_NAME = "de_asset_fb_ad_accounts"


def add():
    # Define form details
    title = "Adding New Facebook Ad Account"
    instructions = [
        "Please provide the following details for the new Facebook Ad Account.",
        "Fields marked with * are required.",
    ]
    fields = [
        {
            "name": "fb_ad_account_id",
            "prompt": "Enter Facebook Ad Account ID (with or without act_)*:",
            "type": "text",
        },
        {
            "name": "fb_ad_account_title",
            "prompt": "Enter Ad Account Title*:",
            "type": "text",
        },
        {
            "name": "fb_ad_account_description",
            "prompt": "Enter Description:",
            "type": "text",
        },
        {
            "name": "fb_app_title",
            "prompt": "Select Facebook App*:",
            "type": "choice",
            "es_index": "de_asset_fb_apps",
            "title_field": "fb_app_title",
            "module": "facebook_apps",
            "store_associated_id": True,
            "condition_affects": True,
        },
        {"name": "fb_app_esid", "type": "hidden"},
        {
            "name": "fb_app_user_title",
            "prompt": "Select Facebook App User*:",
            "type": "choice",
            "es_index": "de_asset_fb_app_users",
            "title_field": "fb_app_user_title",
            "module": "facebook_app_users",
            "store_associated_id": True,
            "filter_field": "fb_app_esid",
            "filter_value_field": "fb_app_esid",
        },
        {"name": "fb_app_user_esid", "type": "hidden"},
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # Strip 'act_' prefix before storage
        if "fb_ad_account_id" in responses:
            responses["fb_ad_account_id"] = strip_ad_account_prefix(responses["fb_ad_account_id"])

        # Index the data into Elasticsearch using the helper function
        if add_document(INDEX_NAME, responses):
            print("Facebook Ad Account added successfully!")
        else:
            print("Failed to add Facebook Ad Account.")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Facebook Ad Accounts found.")
        return

    # List titles and allow user to select one for details
    print("Facebook Ad Accounts:")
    apps = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("fb_ad_account_title", "No Title")
        app_id = hit["_id"]  # Use Elasticsearch's _id for retrieving individual documents
        apps.append((app_id, title))
        print(f"{idx}. {title}")

    # Ensure valid input selection
    while True:
        try:
            selected_idx = int(input("\nEnter the number of the Facebook Ad Account to view: ")) - 1
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

    print("\nFacebook Ad Account Details:")
    print("----------------------")
    for key, value in app.items():
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
    title = "Editing Facebook Ad Account"
    instructions = ["Please review and edit the fields as needed. You can select a specific field to edit."]
    fields = [
        {
            "name": "fb_ad_account_id",
            "prompt": "Facebook Ad Account ID (with or without act_):",
            "type": "text",
        },
        {"name": "fb_ad_account_title", "prompt": "Ad Account Title:", "type": "text"},
        {"name": "fb_ad_account_description", "prompt": "Description:", "type": "text"},
        {
            "name": "fb_app_title",
            "prompt": "Facebook App:",
            "type": "choice",
            "es_index": "de_asset_fb_apps",
            "title_field": "fb_app_title",
            "module": "facebook_apps",
            "store_associated_id": True,
            "condition_affects": True,
        },
        {"name": "fb_app_esid", "type": "hidden"},
        {
            "name": "fb_app_user_title",
            "prompt": "Facebook App User:",
            "type": "choice",
            "es_index": "de_asset_fb_app_users",
            "title_field": "fb_app_user_title",
            "module": "facebook_app_users",
            "store_associated_id": True,
            "filter_field": "fb_app_esid",
            "filter_value_field": "fb_app_esid",
        },
        {"name": "fb_app_user_esid", "type": "hidden"},
    ]

    form = FormHandler(title, instructions, fields)

    # Pre-fill the form responses with the current data
    form.responses = app.copy()

    # Use the review flow to allow the user to select which fields to edit
    action, responses = form.edit_mode()

    if action == "submit":
        # Strip 'act_' prefix before storage
        if "fb_ad_account_id" in responses:
            responses["fb_ad_account_id"] = strip_ad_account_prefix(responses["fb_ad_account_id"])

        # Update only the fields that were changed
        updated_document = {**app, **responses}
        if edit_document(INDEX_NAME, app_id, updated_document):
            print("Facebook Ad Account updated successfully!")
        else:
            print("Failed to update Facebook Ad Account.")
    elif action == "cancel":
        print("Operation canceled.")


def delete(app_id):
    # Confirm deletion
    confirmation = input("Are you sure you want to delete the Facebook Ad Account? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, app_id):
            print("Facebook Ad Account deleted successfully.")
        else:
            print("Failed to delete Facebook Ad Account.")
    else:
        print("Delete operation canceled.")
