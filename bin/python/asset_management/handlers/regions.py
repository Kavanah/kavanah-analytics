from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
)

INDEX_NAME = "de_asset_regions"


def add():
    title = "Adding New Region"
    instructions = [
        "Please provide the following details for the new Region.",
        "Fields marked with * are required.",
    ]
    fields = [
        {"name": "region_title", "prompt": "Enter Region Title*:", "type": "text"},
        {"name": "region_description", "prompt": "Enter Description:", "type": "text"},
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        if add_document(INDEX_NAME, responses):
            print("Region added successfully!")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Regions found.")
        return

    print("Regions:")
    items = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("region_title", "No Title")
        item_id = hit["_id"]
        items.append((item_id, title))
        print(f"{idx}. {title}")

    selected_idx = int(input("\nEnter the number of the Region to view: ")) - 1
    selected_id, _ = items[selected_idx]

    view_details(selected_id)


def view_details(item_id):
    item = get_document_by_id(INDEX_NAME, item_id)
    if not item:
        return

    print("\nRegion Details:")
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

    title = "Editing Region"
    instructions = ["Please review and edit the fields as needed. You can select a specific field to edit."]

    # Pre-fill the form with the existing data
    fields = [
        {
            "name": "region_title",
            "prompt": f"Region Title [{item['region_title']}]:",
            "type": "text",
        },
        {
            "name": "region_description",
            "prompt": f"Description [{item.get('region_description', '')}]:",
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
            print("Region updated successfully!")
    elif action == "cancel":
        print("Operation canceled.")


def delete(item_id):
    confirmation = input(f"Are you sure you want to delete this Region? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, item_id):
            print("Region deleted successfully.")
