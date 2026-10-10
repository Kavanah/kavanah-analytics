from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
)

INDEX_NAME = "de_asset_fb_pages"


def add():
    # Define form details
    title = "Adding New Facebook Page"
    instructions = [
        "Please provide the following details for the new Facebook Page.",
        "Fields marked with * are required.",
    ]
    fields = [
        {
            "name": "fb_page_slug",
            "prompt": "Enter Page Slug (Username)*:",
            "type": "text",
        },
        {"name": "fb_page_id", "prompt": "Enter Facebook Page ID*:", "type": "text"},
        {"name": "fb_page_name", "prompt": "Enter Page Name*:", "type": "text"},
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # Index the data into Elasticsearch using the helper function
        if add_document(INDEX_NAME, responses):
            print("Facebook Page added successfully!")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Facebook Pages found.")
        return

    # List slugs and allow user to select one for details
    print("Facebook Pages:")
    pages = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        slug = source.get("fb_page_slug", "No Slug")
        page_id = hit["_id"]  # Use Elasticsearch's _id for retrieving individual documents
        pages.append((page_id, slug))
        print(f"{idx}. {slug}")

    # Ensure valid input selection
    while True:
        try:
            selected_idx = int(input("\nEnter the number of the Facebook Page to view: ")) - 1
            if 0 <= selected_idx < len(pages):
                selected_id, selected_title = pages[selected_idx]
                break  # Exit the loop if the input is valid
            else:
                print(f"Invalid selection. Please enter a number between 1 and {len(pages)}.")
        except ValueError:
            print("Invalid input. Please enter a valid number.")

    # Pass the Elasticsearch-generated _id to view_details
    view_details(selected_id)


def view_details(page_id):
    page = get_document_by_id(INDEX_NAME, page_id)
    if not page:
        return

    print("\nFacebook Page Details:")
    print("----------------------")
    for key, value in page.items():
        print(f"{key}: {value}")
    print()

    # Options: Edit, Delete, or Back
    print("Options:")
    print("1) Edit")
    print("2) Delete")
    print("3) Back")
    choice = input("Enter your choice: ").strip()

    if choice == "1":
        edit(page_id)
    elif choice == "2":
        delete(page_id)
    elif choice == "3":
        return
    else:
        print("Invalid choice. Returning to main menu.")


def edit(page_id):
    page = get_document_by_id(INDEX_NAME, page_id)
    if not page:
        return

    # Define the form with pre-filled values
    title = "Editing Facebook Page"
    instructions = ["Please review and edit the fields as needed. You can select a specific field to edit."]
    fields = [
        {
            "name": "fb_page_slug",
            "prompt": f"Page Slug (Username) [{page.get('fb_page_slug', '')}]:",
            "type": "text",
        },
        {
            "name": "fb_page_id",
            "prompt": f"Facebook Page ID [{page['fb_page_id']}]:",
            "type": "text",
        },
        {
            "name": "fb_page_name",
            "prompt": f"Page Name [{page['fb_page_name']}]:",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)

    # Pre-fill the form responses with the current data
    form.responses = {key: page.get(key, "") for key in page.keys()}

    # Use the review flow to allow the user to select which fields to edit
    action, responses = form.edit_mode()

    if action == "submit":
        # Update only the fields that were changed
        updated_document = {**page, **responses}
        if edit_document(INDEX_NAME, page_id, updated_document):
            print("Facebook Page updated successfully!")
    elif action == "cancel":
        print("Operation canceled.")


def delete(page_id):
    # Confirm deletion
    confirmation = input(f"Are you sure you want to delete the Facebook Page? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, page_id):
            print("Facebook Page deleted successfully.")
    else:
        print("Delete operation canceled.")
