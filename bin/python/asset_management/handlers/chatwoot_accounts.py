from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.chatwoot_base_url import ChatwootBaseUrlError, vet_account_base_url
from lib.encryption import encrypt, decrypt
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
)

INDEX_NAME = "de_asset_chatwoot_accounts"


def add():
    title = "Adding New Chatwoot Account"
    instructions = [
        "Please provide the following details for the new Chatwoot Account.",
        "Fields marked with * are required.",
    ]
    fields = [
        {
            "name": "chatwoot_account_title",
            "prompt": "Enter Account Title*:",
            "type": "text",
        },
        {
            "name": "chatwoot_base_url",
            "prompt": "Enter Chatwoot Server URL (blank = Chatwoot Cloud):",
            "type": "text",
        },
        {
            "name": "chatwoot_account_id",
            "prompt": "Enter Chatwoot Account ID*:",
            "type": "text",
        },
        {
            "name": "chatwoot_api_token",
            "prompt": "Enter API Access Token*:",
            "type": "text",
            "encrypted": True,
        },
        {
            "name": "chatwoot_account_description",
            "prompt": "Enter Description:",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # public HTTPS server only (blank = Chatwoot Cloud).
        try:
            responses["chatwoot_base_url"] = vet_account_base_url(responses.get("chatwoot_base_url"))
        except ChatwootBaseUrlError as e:
            print(f"Not saved: {e}")
            return
        if add_document(INDEX_NAME, responses):
            print("Chatwoot Account added successfully!")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Chatwoot Accounts found.")
        return

    print("Chatwoot Accounts:")
    items = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("chatwoot_account_title", "No Title")
        item_id = hit["_id"]
        items.append((item_id, title))
        print(f"{idx}. {title}")

    selected_idx = int(input("\nEnter the number of the Chatwoot Account to view: ")) - 1
    selected_id, _ = items[selected_idx]

    view_details(selected_id)


def view_details(item_id):
    item = get_document_by_id(INDEX_NAME, item_id)
    if not item:
        return

    print("\nChatwoot Account Details:")
    for key, value in item.items():
        if key == "chatwoot_api_token":
            value = decrypt(value)
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

    title = "Editing Chatwoot Account"
    instructions = ["Please review and edit the fields as needed."]

    fields = [
        {
            "name": "chatwoot_account_title",
            "prompt": f"Account Title [{item.get('chatwoot_account_title', '')}]:",
            "type": "text",
        },
        {
            "name": "chatwoot_base_url",
            "prompt": f"Chatwoot Server URL, blank = Chatwoot Cloud [{item.get('chatwoot_base_url', '')}]:",
            "type": "text",
        },
        {
            "name": "chatwoot_account_id",
            "prompt": f"Account ID [{item.get('chatwoot_account_id', '')}]:",
            "type": "text",
        },
        {
            "name": "chatwoot_api_token",
            "prompt": "API Access Token:",
            "type": "text",
            "encrypted": True,
        },
        {
            "name": "chatwoot_account_description",
            "prompt": f"Description [{item.get('chatwoot_account_description', '')}]:",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    form.responses = {key: item.get(key, "") for key in item.keys()}

    action, responses = form.edit_mode()

    if action == "submit":
        # moving the account to a different server needs the token re-entered.
        submitted_token = responses.get("chatwoot_api_token") or ""
        try:
            responses["chatwoot_base_url"] = vet_account_base_url(
                responses.get("chatwoot_base_url"),
                existing_doc=item,
                token_reentered=bool(submitted_token) and submitted_token != item.get("chatwoot_api_token"),
            )
        except ChatwootBaseUrlError as e:
            print(f"Not saved: {e}")
            return
        if "chatwoot_api_token" in responses and responses["chatwoot_api_token"]:
            responses["chatwoot_api_token"] = encrypt(responses["chatwoot_api_token"])

        updated_document = {**item, **responses}
        if edit_document(INDEX_NAME, item_id, updated_document):
            print("Chatwoot Account updated successfully!")
    elif action == "cancel":
        print("Operation canceled.")


def delete(item_id):
    confirmation = input("Are you sure you want to delete this Chatwoot Account? (y/n): ").strip().lower()
    if confirmation == "y":
        if delete_asset_with_cleanup(INDEX_NAME, item_id):
            print("Chatwoot Account deleted successfully.")
