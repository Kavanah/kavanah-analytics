#!/bin/bash

CONFIG_FILE="$ders_home/config/master-config.yaml"

# Function to get menu title for viewing configuration settings
ders_get_menu_title_view_config() {
    echo "View Config Vars"
}

# Function to display configuration variables
ders_run_script_view_config() {
    clear
    if [ ! -f "$CONFIG_FILE" ]; then
        echo "No configuration vars setup."
    else
        cat "$CONFIG_FILE"
    fi
    echo ""
    echo "Press any key to return to the menu."
    read -n 1 -s
}

# Function to check if the view configuration task is complete
ders_task_complete_view_config() {
    return 0
}
