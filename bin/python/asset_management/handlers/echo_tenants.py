from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.echo_base_url import EchoSubdomainError, normalize_echo_subdomain
from lib.encryption import encrypt, decrypt
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
)

INDEX_NAME = "de_asset_echo_tenants"


def add():
    title = "Adding New Echo Tenant"
    instructions = [
        "Please provide the following details for the new Echo Tenant.",
        "Fields marked with * are required.",
    ]
    fields = [
        {
            "name": "echo_tenant_title",
            "prompt": "Enter Echo Tenant Title*:",
            "type": "text",
        },
        {
            "name": "echo_tenant_api_key",
            "prompt": "Enter API Key*:",
            "type": "text",
            "encrypted": True,
        },
        {
            "name": "echo_tenant_subdomain",
            "prompt": "Enter Subdomain*:",
            "type": "text",
        },
        {
            "name": "echo_tenant_description",
            "prompt": "Enter Description:",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # the subdomain becomes the importer's request host.
        try:
            responses["echo_tenant_subdomain"] = normalize_echo_subdomain(responses.get("echo_tenant_subdomain"))
        except EchoSubdomainError as e:
            print(f"Not saved: {e}")
            return
        if add_document(INDEX_NAME, responses):
            print("Echo Tenant added successfully!")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Echo Tenants found.")
        return

    print("Echo Tenants:")
    items = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("echo_tenant_title", "No Title")
        item_id = hit["_id"]
        items.append((item_id, title))
        print(f"{idx}. {title}")

    selected_idx = int(input("\nEnter the number of the Echo Tenant to view: ")) - 1
    selected_id, _ = items[selected_idx]

    view_details(selected_id)


def view_details(item_id):
    item = get_document_by_id(INDEX_NAME, item_id)
    if not item:
        return

    print("\nEcho Tenant Details:")
    for key, value in item.items():
        if key == "echo_tenant_api_key":
            value = decrypt(value)  # Decrypt the API key before displaying
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

    title = "Editing Echo Tenant"
    instructions = ["Please review and edit the fields as needed. You can select a specific field to edit."]

    # Pre-fill the form with the existing data
    fields = [
        {
            "name": "echo_tenant_title",
            "prompt": f"Echo Tenant Title [{item['echo_tenant_title']}]:",
            "type": "text",
        },
        {
            "name": "echo_tenant_api_key",
            "prompt": f"API Key [{decrypt(item['echo_tenant_api_key'])}]:",
            "type": "text",
            "encrypted": True,
        },
        {
            "name": "echo_tenant_subdomain",
            "prompt": f"Subdomain [{item['echo_tenant_subdomain']}]:",
            "type": "text",
        },
        {
            "name": "echo_tenant_description",
            "prompt": f"Description [{item.get('echo_tenant_description', '')}]:",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    # Pre-fill the form responses with the current data
    form.responses = {key: item.get(key, "") for key in item.keys()}

    # Use the review flow to allow the user to select which fields to edit
    action, responses = form.edit_mode()

    if action == "submit":
        # the subdomain becomes the importer's request host.
        if "echo_tenant_subdomain" in responses:
            try:
                responses["echo_tenant_subdomain"] = normalize_echo_subdomain(responses["echo_tenant_subdomain"])
            except EchoSubdomainError as e:
                print(f"Not saved: {e}")
                return
        # Encrypt the API key before saving if it was modified
        if "echo_tenant_api_key" in responses:
            responses["echo_tenant_api_key"] = encrypt(responses["echo_tenant_api_key"])

        # Update only the fields that were changed
        updated_document = {**item, **responses}
        if edit_document(INDEX_NAME, item_id, updated_document):
            print("Echo Tenant updated successfully!")
    elif action == "cancel":
        print("Operation canceled.")


def delete(item_id):
    confirmation = input(f"Are you sure you want to delete this Echo Tenant? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, item_id):
            print("Echo Tenant deleted successfully.")
