import os
import subprocess
from lib.encryption import encrypt, decrypt
from lib.es_helpers import get_all_documents
from importlib import import_module


class FormHandler:
    def __init__(self, title, instructions, fields):
        self.title = title
        self.instructions = instructions
        self.fields = fields
        self.responses = {}
        self.active_fields = []

    def clear_screen(self):
        subprocess.run(["clear"], check=True)

    def display_instructions(self):
        self.clear_screen()
        print(self.title)
        print("-" * len(self.title))
        for instruction in self.instructions:
            print(instruction)
        print()

    def should_include_field(self, field):
        condition = field.get("condition")
        if condition:
            if callable(condition):
                return condition(self.responses)
            else:
                return bool(condition)
        return True

    def collect_inputs(self):
        idx = 0
        # Initialize active_fields
        self.active_fields = [field for field in self.fields if self.should_include_field(field)]
        while idx < len(self.active_fields):
            field = self.active_fields[idx]
            name = field["name"]
            # Skip fields that have already been answered
            if name in self.responses:
                idx += 1
                continue

            field_type = field.get("type", "text")

            if field_type == "choice":
                value = self.handle_multiple_choice(field)
                self.responses[name] = value
            elif field_type == "list_input":
                value = self.handle_list_input(field)
                self.responses[name] = value
            elif field_type == "hidden":
                # Hidden fields are not prompted; ensure they have a value if needed
                if name not in self.responses:
                    self.responses[name] = None

                idx += 1
                continue
            else:
                value = input(f"{field['prompt']} ").strip()
                if field.get("encrypted", False):
                    value = encrypt(value)
                self.responses[name] = value

            # If a field affects conditions, re-evaluate active_fields
            if field.get("condition_affects", False):
                # Re-evaluate conditions based on updated responses
                self.active_fields = [f for f in self.fields if self.should_include_field(f)]
                idx = 0
                continue

            idx += 1

    def handle_list_input(self, field):
        items = []
        print(f"\nEnter {field['prompt']} (enter 'done' when finished):")
        while True:
            item = input(" - ").strip()
            if item.lower() == "done":
                if not items:
                    print("You must enter at least one item.")
                    continue
                break
            elif item:
                items.append(item)
            else:
                print("Input cannot be empty. Please enter a valid value or 'done' to finish.")
        return items

    def handle_multiple_choice(self, field):
        choices = field.get("choices", [])
        ids = []  # List to hold the corresponding _esid for each choice

        if field.get("es_index"):
            es_index = field["es_index"]
            title_field = field.get("title_field", "title")
            filter_field = field.get("filter_field")
            filter_value_field = field.get("filter_value_field")
            query = {"match_all": {}}

            if filter_field and filter_value_field:
                # Get the value to filter on from responses
                filter_value = self.responses.get(filter_value_field)
                if not filter_value:
                    print(f"Cannot filter options for {field['prompt']} because {filter_value_field} is missing.")
                    return None
                # Build the query to filter documents
                query = {"term": {f"{filter_field}": filter_value}}
            elif filter_field and "filter_value" in field:
                # Use filter_value directly
                filter_value = field["filter_value"]
                if not filter_value:
                    print(f"Cannot filter options for {field['prompt']} because filter_value is missing.")
                    return None
                query = {"term": {f"{filter_field}": filter_value}}
            else:
                # No filtering applied
                query = {"match_all": {}}

            # Fetch documents from Elasticsearch with optional filtering
            hits = get_all_documents(es_index, query=query)
            if not hits:
                print(f"No options found for {field['prompt']} with the given filter.")
                if field.get("allow_add", True):
                    return self.add_new_option(field)
                else:
                    print(f"Cannot add new options for {field['prompt']}")
                    return None

            choices = [hit["_source"].get(title_field, "No Title") for hit in hits]
            ids = [hit["_id"] for hit in hits]  # Store the _esid for each choice

        if not choices:
            print(f"No options found for {field['prompt']}")
            if field.get("allow_add", True):
                return self.add_new_option(field)
            else:
                print(f"Cannot add new options for {field['prompt']}")
                return None

        while True:
            print(f"\nSelect {field['prompt']}:")
            for idx, option in enumerate(choices, start=1):
                print(f"{idx}) {option}")
            option_count = len(choices)
            if field.get("allow_add", True):
                option_count += 1
                print(f"{option_count}) Add New {field['name']}")
            option_count += 1
            print(f"{option_count}) Go Back")

            choice = input("Enter your choice: ").strip()
            try:
                choice = int(choice)
                if 1 <= choice <= len(choices):
                    selected_title = choices[choice - 1]

                    # Check if the field has the 'store_associated_id' flag and ends with '_title'
                    if field.get("store_associated_id") and field["name"].endswith("_title"):
                        associated_esid_field = field["name"].replace("_title", "_esid")
                        self.responses[associated_esid_field] = ids[choice - 1]  # Save associated ES ID

                    return selected_title
                elif field.get("allow_add", True) and choice == len(choices) + 1:
                    return self.add_new_option(field)
                elif choice == option_count:
                    return None
                else:
                    print("Invalid choice.")
            except ValueError:
                print("Invalid input. Please enter a valid number.")

    def add_new_option(self, field):
        module_name = field.get("module")
        if not module_name:
            print("No module specified for adding new options.")
            return None
        module = import_module(f"asset_management.handlers.{module_name}")
        if hasattr(module, "add"):
            module.add()
            print(f"New {field['name']} added successfully!")
            # Refresh choices
            hits = get_all_documents(field["es_index"])
            if hits:
                latest_added_title = hits[-1]["_source"].get(field.get("title_field", "title"), "No Title")
                return latest_added_title
            else:
                return None
        else:
            print(f"No 'add' method found in module '{module_name}'.")
            return None

    def review_inputs(self):
        while True:
            self.clear_screen()
            print("Review Your Entries")
            print("-" * 20)
            self.active_fields = [field for field in self.fields if self.should_include_field(field)]

            display_fields = []
            for idx, field in enumerate(self.active_fields, start=1):
                name = field["name"]
                # Skip hidden fields
                if field.get("type") == "hidden":
                    continue
                value = self.responses.get(name)
                if field.get("encrypted", False):
                    value = decrypt(value)
                display_fields.append((idx, field["prompt"], value))

            for idx, prompt, value in display_fields:
                if isinstance(value, list):
                    print(f"{idx}. {prompt}")
                    for item in value:
                        print(f"   - {item}")
                else:
                    print(f"{idx}. {prompt} {value}")

            print()
            print("Options:")
            print("1) Submit")
            print("2) Edit")
            print("3) Cancel")
            choice = input("Enter your choice: ").strip()
            if choice == "1":
                return "submit", self.responses
            elif choice == "2":
                self.edit_inputs()
            elif choice == "3":
                return "cancel", self.responses
            else:
                print("Invalid choice. Please try again.")
                input("Press Enter to continue...")

    def edit_inputs(self):
        while True:
            try:
                field_number = int(input("Enter the number of the field you want to edit (or 0 to finish): "))
                if field_number == 0:
                    break
                if 1 <= field_number <= len(self.active_fields):
                    field = self.active_fields[field_number - 1]
                    name = field["name"]
                    if field.get("type") == "choice":
                        new_value = self.handle_multiple_choice(field)
                    elif field.get("type") == "list_input":
                        new_value = self.handle_list_input(field)
                    else:
                        new_value = input(f"Enter new value for {field['prompt']} ").strip()
                        if field.get("encrypted", False):
                            new_value = encrypt(new_value)
                    self.responses[name] = new_value
                    # If editing a field that affects conditions, re-evaluate fields
                    if field.get("condition_affects", False):
                        self.active_fields = [f for f in self.fields if self.should_include_field(f)]
                else:
                    print("Invalid field number.")
            except ValueError:
                print("Please enter a valid number.")

    def run(self):
        self.display_instructions()
        self.collect_inputs()
        return self.review_inputs()

    def edit_mode(self):
        self.active_fields = [field for field in self.fields if self.should_include_field(field)]
        return self.review_inputs()
