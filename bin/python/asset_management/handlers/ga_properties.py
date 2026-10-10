from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
    get_elasticsearch_client,
)
from datetime import datetime

INDEX_NAME = "de_asset_ga_properties"


def get_ga_account_options():
    """Get GA accounts for select dropdown."""
    accounts = get_all_documents("de_asset_ga_accounts")
    options = [{"value": "", "label": "-- None --"}]

    for account in accounts:
        options.append(
            {
                "value": account["_id"],
                "label": f"{account['_source'].get('ga_account_title', 'No Title')} ({account['_source'].get('ga_account_id', 'N/A')})",
            }
        )

    return options


def get_team_options():
    """Get teams for select dropdown."""
    teams = get_all_documents("de_asset_teams")
    options = [{"value": "", "label": "-- None --"}]

    for team in teams:
        options.append(
            {
                "value": team["_id"],
                "label": f"{team['_source'].get('team_title', 'No Title')} ({team['_source'].get('team_esid', 'N/A')})",
            }
        )

    return options


def add():
    title = "Adding New Google Analytics Property"
    instructions = [
        "Please provide the following details for the new Google Analytics Property.",
        "Fields marked with * are required.",
        "Note: Properties can be auto-populated by running export_properties.py",
    ]

    # Get dropdown options
    ga_account_opts = get_ga_account_options()
    team_opts = get_team_options()

    fields = [
        {
            "name": "ga_property_title",
            "prompt": "Enter Property Title*:",
            "type": "text",
        },
        {
            "name": "ga_property_id",
            "prompt": "Enter Property ID (e.g., 123456789)*:",
            "type": "text",
        },
        {
            "name": "ga_property_display_name",
            "prompt": "Enter Display Name*:",
            "type": "text",
        },
        {
            "name": "ga_property_resource_name",
            "prompt": "Enter Resource Name (e.g., properties/123456789):",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # Auto-generate resource name if not provided
        if not responses.get("ga_property_resource_name") and responses.get("ga_property_id"):
            responses["ga_property_resource_name"] = f"properties/{responses['ga_property_id']}"

        # Select GA Account (optional)
        print("\n--- Select GA Account (optional) ---")
        for idx, opt in enumerate(ga_account_opts, start=1):
            print(f"{idx}. {opt['label']}")

        try:
            account_choice = input("\nEnter number (or press Enter to skip): ").strip()
            if account_choice:
                account_idx = int(account_choice) - 1
                if 0 <= account_idx < len(ga_account_opts):
                    selected_account_esid = ga_account_opts[account_idx]["value"]
                    if selected_account_esid:
                        responses["ga_account_esid"] = selected_account_esid

                        # Get account details
                        account = get_document_by_id("de_asset_ga_accounts", selected_account_esid)
                        if account:
                            responses["ga_account_id"] = account.get("ga_account_id", "")
                            responses["ga_account_title"] = account.get("ga_account_title", "")
        except ValueError:
            print("Invalid input, skipping account selection.")

        # Select Team (optional)
        print("\n--- Select Team (optional) ---")
        for idx, opt in enumerate(team_opts, start=1):
            print(f"{idx}. {opt['label']}")

        try:
            team_choice = input("\nEnter number (or press Enter to skip): ").strip()
            if team_choice:
                team_idx = int(team_choice) - 1
                if 0 <= team_idx < len(team_opts):
                    selected_team_esid = team_opts[team_idx]["value"]
                    if selected_team_esid:
                        responses["team_esid"] = selected_team_esid

                        # Get team details
                        team = get_document_by_id("de_asset_teams", selected_team_esid)
                        if team:
                            responses["team_title"] = team.get("team_title", "")
        except ValueError:
            print("Invalid input, skipping team selection.")

        # Add timestamp field required by Elasticsearch
        responses["@timestamp"] = datetime.utcnow().isoformat()

        # Index the data into Elasticsearch
        if add_document(INDEX_NAME, responses):
            print("Google Analytics Property added successfully!")
        else:
            print("Failed to add Google Analytics Property.")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Google Analytics Properties found.")
        return

    # List titles and allow user to select one for details
    print("Google Analytics Properties:")
    properties = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("ga_property_title", "No Title")
        property_id = hit["_id"]
        team_title = source.get("team_title", "No Team")
        properties.append((property_id, title))
        print(f"{idx}. {title} (Team: {team_title})")

    while True:
        try:
            choice = input("\nEnter number to view details, 'e' to edit, 'd' to delete, or 'b' to go back: ").strip().lower()
            if choice == "b":
                return
            elif choice == "e":
                edit_choice = int(input("Enter number to edit: ").strip())
                if 1 <= edit_choice <= len(properties):
                    property_id, _ = properties[edit_choice - 1]
                    edit_property(property_id)
                    return
            elif choice == "d":
                delete_choice = int(input("Enter number to delete: ").strip())
                if 1 <= delete_choice <= len(properties):
                    property_id, title = properties[delete_choice - 1]
                    confirm = input(f"Are you sure you want to delete '{title}'? (yes/no): ").strip().lower()
                    if confirm == "yes":
                        if delete_asset_with_cleanup(INDEX_NAME, property_id):
                            print(f"Deleted '{title}' successfully.")
                        else:
                            print(f"Failed to delete '{title}'.")
                    return
            else:
                choice_int = int(choice)
                if 1 <= choice_int <= len(properties):
                    property_id, _ = properties[choice_int - 1]
                    display_property_details(property_id)
                else:
                    print("Invalid choice. Please try again.")
        except ValueError:
            print("Invalid input. Please enter a number, 'e', 'd', or 'b'.")


def display_property_details(property_id):
    prop = get_document_by_id(INDEX_NAME, property_id)
    if not prop:
        print("Property not found.")
        return

    print("\n" + "=" * 60)
    print("Google Analytics Property Details")
    print("=" * 60)
    print(f"Property Title: {prop.get('ga_property_title', 'N/A')}")
    print(f"Property ID: {prop.get('ga_property_id', 'N/A')}")
    print(f"Display Name: {prop.get('ga_property_display_name', 'N/A')}")
    print(f"Resource Name: {prop.get('ga_property_resource_name', 'N/A')}")
    print(f"\nGA Account ID: {prop.get('ga_account_id', 'N/A')}")
    print(f"GA Account Title: {prop.get('ga_account_title', 'N/A')}")
    print(f"\nTeam: {prop.get('team_title', 'Not Assigned')}")
    print(f"Team ESID: {prop.get('team_esid', 'N/A')}")
    print("=" * 60 + "\n")


def edit_property(property_id):
    prop = get_document_by_id(INDEX_NAME, property_id)
    if not prop:
        print("Property not found.")
        return

    print("\n" + "=" * 60)
    print(f"Editing: {prop.get('ga_property_title', 'N/A')}")
    print("=" * 60)
    print("Leave fields blank to keep current values.\n")

    # Property Title
    new_title = input(f"Property Title [{prop.get('ga_property_title', '')}]: ").strip()
    if new_title:
        prop["ga_property_title"] = new_title

    # Property ID
    new_property_id = input(f"Property ID [{prop.get('ga_property_id', '')}]: ").strip()
    if new_property_id:
        prop["ga_property_id"] = new_property_id
        # Auto-update resource name
        prop["ga_property_resource_name"] = f"properties/{new_property_id}"

    # Display Name
    new_display_name = input(f"Display Name [{prop.get('ga_property_display_name', '')}]: ").strip()
    if new_display_name:
        prop["ga_property_display_name"] = new_display_name

    # GA Account Selection
    print("\n--- Change GA Account? (y/n) ---")
    change_account = input("Current: " + prop.get("ga_account_title", "Not Set") + " (y/n): ").strip().lower()
    if change_account == "y":
        ga_account_opts = get_ga_account_options()
        for idx, opt in enumerate(ga_account_opts, start=1):
            print(f"{idx}. {opt['label']}")

        try:
            account_choice = input("\nEnter number: ").strip()
            if account_choice:
                account_idx = int(account_choice) - 1
                if 0 <= account_idx < len(ga_account_opts):
                    selected_account_esid = ga_account_opts[account_idx]["value"]
                    if selected_account_esid:
                        prop["ga_account_esid"] = selected_account_esid

                        # Get account details
                        account = get_document_by_id("de_asset_ga_accounts", selected_account_esid)
                        if account:
                            prop["ga_account_id"] = account.get("ga_account_id", "")
                            prop["ga_account_title"] = account.get("ga_account_title", "")
                    else:
                        # Clear account
                        prop["ga_account_esid"] = None
                        prop["ga_account_id"] = None
                        prop["ga_account_title"] = None
        except ValueError:
            print("Invalid input, keeping current account.")

    # Team Selection
    print("\n--- Change Team Assignment? (y/n) ---")
    change_team = input("Current: " + prop.get("team_title", "Not Set") + " (y/n): ").strip().lower()
    if change_team == "y":
        team_opts = get_team_options()
        for idx, opt in enumerate(team_opts, start=1):
            print(f"{idx}. {opt['label']}")

        try:
            team_choice = input("\nEnter number: ").strip()
            if team_choice:
                team_idx = int(team_choice) - 1
                if 0 <= team_idx < len(team_opts):
                    selected_team_esid = team_opts[team_idx]["value"]
                    if selected_team_esid:
                        prop["team_esid"] = selected_team_esid

                        # Get team details
                        team = get_document_by_id("de_asset_teams", selected_team_esid)
                        if team:
                            prop["team_title"] = team.get("team_title", "")
                    else:
                        # Clear team
                        prop["team_esid"] = None
                        prop["team_title"] = None
        except ValueError:
            print("Invalid input, keeping current team.")

    # Confirm changes
    confirm = input("\nSave changes? (yes/no): ").strip().lower()
    if confirm == "yes":
        # Update timestamp
        prop["@timestamp"] = datetime.utcnow().isoformat()

        if edit_document(INDEX_NAME, property_id, prop):
            print("Google Analytics Property updated successfully!")
        else:
            print("Failed to update Google Analytics Property.")
    else:
        print("Changes discarded.")
