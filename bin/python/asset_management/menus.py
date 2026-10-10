# menus.py

import sys
import os
import subprocess
from asset_management.config import menu_config


class MenuManager:
    def __init__(self):
        self.menu_stack = []
        self.current_menu = "main_menu"
        self.previous_menu = None
        self.item_context = {}

    def run(self):
        while True:
            self.clear_screen()
            menu = menu_config[self.current_menu]
            self.display_menu(menu)
            choice = input("Enter your choice: ").strip()
            self.handle_choice(menu, choice)

    def clear_screen(self):
        # Clear the terminal screen
        subprocess.run(["clear"], check=True)

    def display_menu(self, menu):
        # Display the current menu
        title = menu["title"].format(**self.item_context)
        print("\n" + title)
        print("-" * len(title))
        for idx, option in enumerate(menu["options"], start=1):
            text = option["text"].format(**self.item_context)
            print(f"{idx}) {text}")

    def handle_choice(self, menu, choice):
        # Handle the user's menu choice
        try:
            choice = int(choice) - 1
        except ValueError:
            print("Invalid input, please enter a number.")
            input("Press Enter to continue...")
            return

        if 0 <= choice < len(menu["options"]):
            option = menu["options"][choice]
            if "action" in option:
                action = option["action"]
                item = option.get("item", "").format(**self.item_context) if "item" in option else None
                handler_name = option.get("handler", self.item_context.get("handler"))
                self.perform_action(action, item, handler_name)
            elif "next_menu" in option:
                if option["next_menu"] == "exit":
                    print("Exiting...")
                    sys.exit()
                elif option["next_menu"] == "previous":
                    if self.menu_stack:
                        self.current_menu = self.menu_stack.pop()
                else:
                    self.menu_stack.append(self.current_menu)
                    self.current_menu = option["next_menu"]
                    if "item" in option:
                        self.item_context["item"] = option["item"]
                    if "handler" in option:
                        self.item_context["handler"] = option["handler"]
            else:
                print("Invalid option configuration.")
        else:
            print("Invalid choice. Please try again.")

    def perform_action(self, action, item, handler_name):
        # Perform the specified action
        self.clear_screen()
        try:
            handler_module = __import__(f"asset_management.handlers.{handler_name}", fromlist=[""])
        except ImportError:
            print(f"No handler found for '{handler_name}'.")
            input("Press Enter to continue...")
            return
        action_func = getattr(handler_module, action, None)
        if callable(action_func):
            action_func()
        else:
            print(f"No action '{action}' defined in handler '{handler_name}'.")
        input("Press Enter to continue...")
        # Return to the previous menu after action
        if self.menu_stack:
            self.current_menu = self.menu_stack.pop()
