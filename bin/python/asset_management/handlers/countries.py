from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
)

INDEX_NAME = "de_asset_countries"


def add():
    title = "Adding New Country"
    instructions = [
        "Please provide the following details for the new Country.",
        "Fields marked with * are required.",
    ]
    fields = [
        {"name": "country_title", "prompt": "Enter Country Title*:", "type": "text"},
        {"name": "country_description", "prompt": "Enter Description:", "type": "text"},
        {
            "name": "country_iso3_code",
            "prompt": "Enter ISO 3-Letter Code*:",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        if add_document(INDEX_NAME, responses):
            print("Country added successfully!")
    elif action == "cancel":
        print("Operation canceled.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Countries found.")
        return

    print("Countries:")
    items = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("country_title", "No Title")
        item_id = hit["_id"]
        items.append((item_id, title))
        print(f"{idx}. {title}")

    selected_idx = int(input("\nEnter the number of the Country to view: ")) - 1
    selected_id, _ = items[selected_idx]

    view_details(selected_id)


def view_details(item_id):
    item = get_document_by_id(INDEX_NAME, item_id)
    if not item:
        return

    print("\nCountry Details:")
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

    title = "Editing Country"
    instructions = ["Please review and edit the fields as needed. You can select a specific field to edit."]

    # Pre-fill the form with the existing data
    fields = [
        {
            "name": "country_title",
            "prompt": f"Country Title [{item['country_title']}]:",
            "type": "text",
        },
        {
            "name": "country_description",
            "prompt": f"Description [{item.get('country_description', '')}]:",
            "type": "text",
        },
        {
            "name": "country_iso3_code",
            "prompt": f"ISO 3-Letter Code [{item.get('country_iso3_code', '')}]:",
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
            print("Country updated successfully!")
    elif action == "cancel":
        print("Operation canceled.")


def delete(item_id):
    confirmation = input(f"Are you sure you want to delete this Country? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, item_id):
            print("Country deleted successfully.")
