from asset_management.forms import FormHandler
from asset_management.handlers import delete_asset_with_cleanup
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    edit_document,
    get_elasticsearch_client,
)
import os
import json
import subprocess
from datetime import datetime

INDEX_NAME = "de_asset_google_ads_customers"


def add():
    title = "Adding New Google Ads Customer"
    instructions = [
        "Please provide the following details for the new Google Ads Customer.",
        "Fields marked with * are required.",
        "Note: API credentials are configured in /app/reporting_configs/google-ads.yaml",
    ]
    fields = [
        {
            "name": "google_ads_customer_title",
            "prompt": "Enter Customer Title*:",
            "type": "text",
        },
        {
            "name": "google_ads_customer_id",
            "prompt": "Enter Customer ID (format: 123-456-7890)*:",
            "type": "text",
        },
    ]

    form = FormHandler(title, instructions, fields)
    action, responses = form.run()

    if action == "submit":
        # Add timestamp field required by Elasticsearch
        responses["@timestamp"] = datetime.utcnow().isoformat()

        # Index the data into Elasticsearch
        if add_document(INDEX_NAME, responses):
            print("Google Ads Customer added successfully!")
        else:
            print("Failed to add Google Ads Customer.")
    elif action == "cancel":
        print("Operation canceled.")
    else:
        print("Unexpected action.")


def view():
    hits = get_all_documents(INDEX_NAME)

    if not hits:
        print("No Google Ads Customers found.")
        return

    # List titles and allow user to select one for details
    print("Google Ads Customers:")
    customers = []
    for idx, hit in enumerate(hits, start=1):
        source = hit["_source"]
        title = source.get("google_ads_customer_title", "No Title")
        customer_id = hit["_id"]
        customers.append((customer_id, title))
        print(f"{idx}. {title}")

    while True:
        try:
            choice = input("\nEnter number to view details, 'e' to edit, 'd' to delete, or 'b' to go back: ").strip().lower()
            if choice == "b":
                return
            elif choice == "e":
                edit_choice = int(input("Enter number to edit: ").strip())
                if 1 <= edit_choice <= len(customers):
                    customer_id, _ = customers[edit_choice - 1]
                    edit_customer(customer_id)
                    return
            elif choice == "d":
                delete_choice = int(input("Enter number to delete: ").strip())
                if 1 <= delete_choice <= len(customers):
                    customer_id, title = customers[delete_choice - 1]
                    confirm = input(f"Are you sure you want to delete '{title}'? (yes/no): ").strip().lower()
                    if confirm == "yes":
                        if delete_asset_with_cleanup(INDEX_NAME, customer_id):
                            print(f"Deleted '{title}' successfully.")
                        else:
                            print(f"Failed to delete '{title}'.")
                    return
            else:
                choice_int = int(choice)
                if 1 <= choice_int <= len(customers):
                    customer_id, _ = customers[choice_int - 1]
                    display_customer_details(customer_id)
                else:
                    print("Invalid choice. Please try again.")
        except ValueError:
            print("Invalid input. Please enter a number, 'e', 'd', or 'b'.")


def display_customer_details(customer_id):
    customer = get_document_by_id(INDEX_NAME, customer_id)
    if not customer:
        print("Customer not found.")
        return

    print("\n" + "=" * 50)
    print("Google Ads Customer Details")
    print("=" * 50)
    print(f"Customer Title: {customer.get('google_ads_customer_title', 'N/A')}")
    print(f"Customer ID: {customer.get('google_ads_customer_id', 'N/A')}")
    print("\nNote: API credentials are configured in /app/reporting_configs/google-ads.yaml")
    print("=" * 50 + "\n")


def edit_customer(customer_id):
    customer = get_document_by_id(INDEX_NAME, customer_id)
    if not customer:
        print("Customer not found.")
        return

    print("\n" + "=" * 50)
    print(f"Editing: {customer.get('google_ads_customer_title', 'N/A')}")
    print("=" * 50)
    print("Leave fields blank to keep current values.\n")

    # Customer Title
    new_title = input(f"Customer Title [{customer.get('google_ads_customer_title', '')}]: ").strip()
    if new_title:
        customer["google_ads_customer_title"] = new_title

    # Customer ID
    new_customer_id = input(f"Customer ID [{customer.get('google_ads_customer_id', '')}]: ").strip()
    if new_customer_id:
        customer["google_ads_customer_id"] = new_customer_id

    # Confirm changes
    confirm = input("\nSave changes? (yes/no): ").strip().lower()
    if confirm == "yes":
        # Update timestamp
        customer["@timestamp"] = datetime.utcnow().isoformat()

        if edit_document(INDEX_NAME, customer_id, customer):
            print("Google Ads Customer updated successfully!")
        else:
            print("Failed to update Google Ads Customer.")
    else:
        print("Changes discarded.")
