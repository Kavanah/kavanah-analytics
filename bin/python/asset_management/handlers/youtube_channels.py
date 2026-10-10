"""
YouTube Channels Asset Management Handler

Manages YouTube channel assets for organic analytics imports.
Channels are linked to teams and require service account access.
"""

from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
)
from datetime import datetime

INDEX_NAME = "de_asset_youtube_channels"


def add():
    """Add a new YouTube channel to the asset system."""
    title = "Adding New YouTube Channel"
    instructions = [
        "Please provide the following details for the new YouTube Channel.",
        "Fields marked with * are required.",
        "",
        "How to find Channel ID:",
        "1. Go to YouTube Studio (studio.youtube.com)",
        "2. Click Settings → Channel → Advanced settings",
        "3. Copy the Channel ID (format: UCxxxxxxxxxxxxxx)",
        "",
        "Note: You must grant service account access to the channel",
        "      Service account: youtube-analytics-sa@your-project.iam.gserviceaccount.com",
    ]

    fields = [
        {
            "name": "youtube_channel_title",
            "prompt": "Enter Channel Title*:",
            "type": "text",
        },
        {
            "name": "youtube_channel_id",
            "prompt": "Enter Channel ID (UCxxxxxxxxxxxxxx)*:",
            "type": "text",
        },
        {
            "name": "youtube_channel_handle",
            "prompt": "Enter Channel Handle (@username):",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # Validate channel ID format
        channel_id = responses.get("youtube_channel_id", "")
        if not channel_id.startswith("UC") or len(channel_id) != 24:
            print("\n⚠️  Warning: Channel ID should start with 'UC' and be 24 characters long.")
            print("   Example: UCxxxxxxxxxxxxxx")
            confirm = input("   Continue anyway? (yes/no): ").strip().lower()
            if confirm != "yes":
                print("Operation canceled.")
                return

        # Add timestamp
        responses["@timestamp"] = datetime.utcnow().isoformat()

        # Index to Elasticsearch
        if add_document(INDEX_NAME, responses):
            print("\n✓ YouTube Channel added successfully!")
            print("\n⚠️  Important Next Steps:")
            print("   1. Grant service account access in YouTube Studio:")
            print("      - Go to studio.youtube.com for this channel")
            print("      - Settings → Permissions → Invite")
            print("      - Add: youtube-analytics-sa@your-project.iam.gserviceaccount.com")
            print("      - Grant 'Viewer' access")
            print("   2. Link this channel to a team:")
            print("      - Teams → Edit Team → YouTube Integration")
        else:
            print("✗ Failed to add YouTube Channel.")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    """View and manage YouTube channels."""
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("\nNo YouTube Channels found.")
        print("\nTo add a channel:")
        print("1. Create service account in Google Cloud Console")
        print("2. Add channel here")
        print("3. Grant service account access in YouTube Studio")
        return

    # List channels
    print("\nYouTube Channels:")
    print("=" * 70)
    channels = []

    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("youtube_channel_title", "No Title")
        channel_id = hit["_id"]
        handle = source.get("youtube_channel_handle", "N/A")
        channels.append((channel_id, title))
        print(f"{idx}. {title} ({handle})")

    print("=" * 70)

    while True:
        try:
            choice = input("\nEnter number to view details, 'e' to edit, 'd' to delete, or 'b' to go back: ").strip().lower()

            if choice == "b":
                return
            elif choice == "e":
                edit_choice = int(input("Enter number to edit: ").strip())
                if 1 <= edit_choice <= len(channels):
                    channel_id, _ = channels[edit_choice - 1]
                    edit_channel(channel_id)
                    return
            elif choice == "d":
                delete_choice = int(input("Enter number to delete: ").strip())
                if 1 <= delete_choice <= len(channels):
                    channel_id, title = channels[delete_choice - 1]
                    confirm = input(f"Are you sure you want to delete '{title}'? (yes/no): ").strip().lower()
                    if confirm == "yes":
                        if delete_asset_with_cleanup(INDEX_NAME, channel_id):
                            print(f"✓ Deleted '{title}' successfully.")
                        else:
                            print(f"✗ Failed to delete '{title}'.")
                    return
            else:
                choice_int = int(choice)
                if 1 <= choice_int <= len(channels):
                    channel_id, _ = channels[choice_int - 1]
                    display_channel_details(channel_id)
                else:
                    print("Invalid choice. Please try again.")
        except ValueError:
            print("Invalid input. Please enter a number, 'e', 'd', or 'b'.")


def display_channel_details(channel_id):
    """Display detailed information for a YouTube channel."""
    channel = get_document_by_id(INDEX_NAME, channel_id)
    if not channel:
        print("Channel not found.")
        return

    print("\n" + "=" * 70)
    print("YouTube Channel Details")
    print("=" * 70)
    print(f"Channel Title:  {channel.get('youtube_channel_title', 'N/A')}")
    print(f"Channel ID:     {channel.get('youtube_channel_id', 'N/A')}")
    print(f"Channel Handle: {channel.get('youtube_channel_handle', 'N/A')}")
    print(f"ESID:           {channel_id}")

    # Show linked teams
    print("\n--- Access Configuration ---")
    print("Service account must have access in YouTube Studio:")
    print("  1. Go to: studio.youtube.com (for this channel)")
    print("  2. Settings → Permissions")
    print("  3. Verify service account email is listed with 'Viewer' access")

    # Check if linked to any team
    from lib.es_helpers import search_index

    try:
        team_results = search_index(
            index_name="de_asset_teams",
            query={
                "bool": {
                    "must": [
                        # bare fields (explicit keyword post-reindex).
                        {"term": {"team_youtube_integration_status": "Active"}},
                        {"term": {"youtube_channel_esid": channel_id}},
                    ]
                }
            },
            size=10,
        )

        team_hits = team_results.get("hits", {}).get("hits", [])
        if team_hits:
            print("\n--- Linked Teams ---")
            for team_hit in team_hits:
                team = team_hit["_source"]
                print(f"  • {team.get('team_title', 'Unknown Team')}")
        else:
            print("\n⚠️  Warning: Channel not linked to any team")
            print("   Link in: Teams → Edit Team → YouTube Integration")
    except Exception as e:
        print(f"\n⚠️  Could not check team linkage: {e}")

    print("=" * 70 + "\n")


def edit_channel(channel_id):
    """Edit an existing YouTube channel."""
    channel = get_document_by_id(INDEX_NAME, channel_id)
    if not channel:
        print("Channel not found.")
        return

    print("\n" + "=" * 70)
    print(f"Editing: {channel.get('youtube_channel_title', 'N/A')}")
    print("=" * 70)
    print("Leave fields blank to keep current values.\n")

    # Channel Title
    current_title = channel.get("youtube_channel_title", "")
    new_title = input(f"Channel Title [{current_title}]: ").strip()
    if new_title:
        channel["youtube_channel_title"] = new_title

    # Channel ID
    current_channel_id = channel.get("youtube_channel_id", "")
    new_channel_id = input(f"Channel ID [{current_channel_id}]: ").strip()
    if new_channel_id:
        if not new_channel_id.startswith("UC") or len(new_channel_id) != 24:
            print("\n⚠️  Warning: Channel ID should start with 'UC' and be 24 characters long.")
            confirm = input("   Continue anyway? (yes/no): ").strip().lower()
            if confirm != "yes":
                print("Changes discarded.")
                return
        channel["youtube_channel_id"] = new_channel_id

    # Channel Handle
    current_handle = channel.get("youtube_channel_handle", "")
    new_handle = input(f"Channel Handle [{current_handle}]: ").strip()
    if new_handle:
        # Ensure handle starts with @
        if new_handle and not new_handle.startswith("@"):
            new_handle = "@" + new_handle
        channel["youtube_channel_handle"] = new_handle

    # Confirm changes
    confirm = input("\nSave changes? (yes/no): ").strip().lower()
    if confirm == "yes":
        # Update timestamp
        channel["@timestamp"] = datetime.utcnow().isoformat()

        if edit_document(INDEX_NAME, channel_id, channel):
            print("✓ YouTube Channel updated successfully!")
        else:
            print("✗ Failed to update YouTube Channel.")
    else:
        print("Changes discarded.")


# Convenience function for menu integration
def menu():
    """Display YouTube Channels menu."""
    while True:
        print("\n" + "=" * 70)
        print("YouTube Channels Management")
        print("=" * 70)
        print("1. Add New Channel")
        print("2. View/Edit/Delete Channels")
        print("3. Back to Main Menu")
        print("=" * 70)

        choice = input("Enter your choice: ").strip()

        if choice == "1":
            add()
        elif choice == "2":
            view()
        elif choice == "3":
            break
        else:
            print("Invalid choice. Please try again.")
