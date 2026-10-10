from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
)

INDEX_NAME = "de_asset_dashboards"


def add():
    title = "Adding New Dashboard"
    instructions = [
        "Please provide the following details for the new Dashboard.",
        "Fields marked with * are required.",
    ]
    fields = [
        {"name": "dashboard_id", "prompt": "Enter Dashboard ID*:", "type": "text"},
        {
            "name": "dashboard_title",
            "prompt": "Enter Dashboard Title*:",
            "type": "text",
        },
        {
            "name": "dashboard_description",
            "prompt": "Enter Description:",
            "type": "text",
        },
        {
            "name": "space_id",
            "prompt": "Enter Kibana Space (default: default):",
            "type": "text",
        },
        {
            "name": "visibility_mode",
            "prompt": "Enter Visibility Mode (show_all/show_teams/hide)*:",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # Set default space_id if not provided
        if not responses.get("space_id"):
            responses["space_id"] = "default"

        if add_document(INDEX_NAME, responses):
            print("Dashboard added successfully!")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Dashboards found.")
        return

    print("Dashboards:")
    items = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("dashboard_title", "No Title")
        visibility = source.get("visibility_mode", "Unknown")
        item_id = hit["_id"]
        items.append((item_id, title))
        print(f"{idx}. {title} ({visibility})")

    selected_idx = int(input("\nEnter the number of the Dashboard to view: ")) - 1
    selected_id, _ = items[selected_idx]

    view_details(selected_id)


def view_details(item_id):
    item = get_document_by_id(INDEX_NAME, item_id)
    if not item:
        return

    print("\nDashboard Details:")
    for key, value in item.items():
        print(f"{key}: {value}")

    print("1) Edit\n2) Delete\n3) Back")
    choice = input("Enter your choice: ").strip()

    if choice == "1":
        edit(item_id)
    elif choice == "2":
        delete(item_id)


def edit(item_id):
    item = get_document_by_id(INDEX_NAME, item_id)
    if not item:
        return

    title = "Editing Dashboard"
    instructions = ["Please review and edit the fields as needed. You can select a specific field to edit."]

    # Pre-fill the form with the existing data
    fields = [
        {
            "name": "dashboard_id",
            "prompt": f"Dashboard ID [{item.get('dashboard_id', '')}]:",
            "type": "text",
        },
        {
            "name": "dashboard_title",
            "prompt": f"Dashboard Title [{item.get('dashboard_title', '')}]:",
            "type": "text",
        },
        {
            "name": "dashboard_description",
            "prompt": f"Description [{item.get('dashboard_description', '')}]:",
            "type": "text",
        },
        {
            "name": "space_id",
            "prompt": f"Kibana Space [{item.get('space_id', 'default')}]:",
            "type": "text",
        },
        {
            "name": "visibility_mode",
            "prompt": f"Visibility Mode [{item.get('visibility_mode', '')}]:",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    # Pre-fill the form responses with the current data
    form.responses = {key: item.get(key, "") for key in item.keys()}

    # Use the review flow to allow the user to select which fields to edit
    action, responses = form.edit_mode()

    if action == "submit":
        # Update only the fields that were changed
        updated_document = {**item, **responses}
        if edit_document(INDEX_NAME, item_id, updated_document):
            print("Dashboard updated successfully!")
    elif action == "cancel":
        print("Operation canceled.")


def delete(item_id):
    confirmation = input(f"Are you sure you want to delete this Dashboard? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, item_id):
            print("Dashboard deleted successfully.")
