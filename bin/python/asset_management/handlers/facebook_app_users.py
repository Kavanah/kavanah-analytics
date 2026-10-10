import yaml
from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
    get_elasticsearch_client,
)

INDEX_NAME = "de_asset_fb_app_users"


def generate_random_string(length):
    """Random alphanumeric string of the given length.

    one CSPRNG generator for every OAuth slug/state — see
    ``lib.oauth_state`` (this used to be a ``random.choices`` copy of
    ``FormService.generate_random_string``).
    """
    from lib.oauth_state import random_alnum

    return random_alnum(length)


def generate_unique_auth_url():
    """Generate a unique 5-character string for fb_app_user_auth_url."""
    es = get_elasticsearch_client()
    if es is None:
        print("Failed to establish Elasticsearch connection.")
        return None

    while True:
        auth_url = generate_random_string(15)
        # Check if this auth_url is already in use
        query = {"query": {"term": {"fb_app_user_auth_url": auth_url}}}
        result = es.search(index=INDEX_NAME, body=query)
        if result["hits"]["total"]["value"] == 0:
            return auth_url
        # Else, generate another one


def add():
    title = "Adding New Facebook App User"
    instructions = [
        "Please provide the following details for the new Facebook App User.",
        "Fields marked with * are required.",
    ]
    fields = [
        {
            "name": "fb_app_user_title",
            "prompt": "Enter Facebook App User Title*:",
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
        },
        {"name": "fb_app_esid", "type": "hidden"},
        {
            "name": "fb_app_user_role",
            "prompt": "Select User Role*:",
            "type": "choice",
            "choices": ["Ads", "Pages"],
            "allow_add": False,
        },
        {"name": "fb_app_user_auth_url", "type": "hidden"},
        {"name": "fb_app_user_auth_state", "type": "hidden"},
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # Generate 'fb_app_user_auth_url' and 'fb_app_user_auth_state'
        fb_app_user_auth_url = generate_unique_auth_url()
        fb_app_user_auth_state = generate_random_string(25)

        responses["fb_app_user_auth_url"] = fb_app_user_auth_url
        responses["fb_app_user_auth_state"] = fb_app_user_auth_state

        # Index the data into Elasticsearch
        if add_document(INDEX_NAME, responses):
            print("Facebook App User added successfully!")
        else:
            print("Failed to add Facebook App User.")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Facebook App Users found.")
        return

    # List titles and allow user to select one for details
    print("Facebook App Users:")
    app_users = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("fb_app_user_title", "--Unknown User--")
        app = source.get("fb_app_title", "")
        app_user_id = hit["_id"]
        app_users.append((app_user_id, title))
        print(f"{idx}. {title} ({app})")

    while True:
        try:
            selected_idx = int(input("\nEnter the number of the Facebook App User to view: ")) - 1
            if 0 <= selected_idx < len(app_users):
                selected_id, _ = app_users[selected_idx]
                break
            else:
                print(f"Invalid selection. Please enter a number between 1 and {len(app_users)}.")
        except ValueError:
            print("Invalid input. Please enter a valid number.")

    view_details(selected_id)


def view_details(app_user_id):
    app_user = get_document_by_id(INDEX_NAME, app_user_id)
    if not app_user:
        return

    base_url = get_fb_oauth_base_url()

    print("\nFacebook App User Details:")
    print("----------------------")
    # Only display the specified fields
    fields_to_display = [
        "fb_app_user_title",
        "fb_app_title",
        "fb_app_user_auth_url",
        "fb_app_user_role",
    ]
    app_id = app_user.get("fb_app_esid")
    for key in fields_to_display:
        value = app_user.get(key)
        # Modify the 'fb_app_user_auth_url' to include the base_url if it exists
        if key == "fb_app_user_auth_url" and value:
            value = f"https://{base_url}/{app_id}/{value}" if base_url else f"/{value}"
        print(f"{key}: {value}")
    print()

    print("1) Edit\n2) Delete\n3) Back")
    choice = input("Enter your choice: ").strip()

    if choice == "1":
        edit(app_user_id)
    elif choice == "2":
        delete(app_user_id)
    elif choice == "3":
        return
    else:
        print("Invalid choice. Returning to main menu.")


# Add this helper function to read the base_url from the YAML file
def get_fb_oauth_base_url():
    """Retrieve the base URL from the master-config.yaml file."""
    config_path = "/app/reporting_configs/master-config.yaml"
    try:
        with open(config_path, "r") as file:
            config = yaml.safe_load(file)
            base_url = config.get("fboauth", {}).get("hostname", "")
            return base_url if base_url else ""
    except Exception as e:
        print(f"Error reading base URL from config: {e}")
        return ""


def edit(app_user_id):
    app_user = get_document_by_id(INDEX_NAME, app_user_id)
    if not app_user:
        return

    title = "Editing Facebook App User"
    instructions = ["Please review and edit the fields as needed. You can select a specific field to edit."]

    fields = [
        {
            "name": "fb_app_user_title",
            "prompt": "Facebook App User Title:",
            "type": "text",
        },
        {
            "name": "fb_app_title",
            "prompt": "Facebook App:",
            "type": "choice",
            "es_index": "de_asset_fb_apps",
            "title_field": "fb_app_title",
            "module": "facebook_apps",
            "store_associated_id": True,
        },
        {"name": "fb_app_esid", "type": "hidden"},
        {
            "name": "fb_app_user_role",
            "prompt": "User Role:",
            "type": "choice",
            "choices": ["Ads", "Pages"],
            "allow_add": False,
        },
        {"name": "fb_app_user_auth_url", "type": "hidden"},
        {"name": "fb_app_user_auth_state", "type": "hidden"},
    ]

    form = FormHandler(title, instructions, fields)
    form.responses = app_user.copy()

    action, responses = form.edit_mode()

    if action == "submit":
        updated_document = {**app_user, **responses}
        if edit_document(INDEX_NAME, app_user_id, updated_document):
            print("Facebook App User updated successfully!")
        else:
            print("Failed to update Facebook App User.")
    else:
        print("Operation canceled.")


def delete(app_user_id):
    confirmation = input(f"Are you sure you want to delete the Facebook App User? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, app_user_id):
            print("Facebook App User deleted successfully.")
        else:
            print("Failed to delete Facebook App User.")
    else:
        print("Deletion canceled.")
