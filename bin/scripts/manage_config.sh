#!/bin/bash

ders_CONFIG_FILE="$ders_home/config/master-config.yaml"
if [ ! -f "$ders_CONFIG_FILE" ]; then
    mkdir -p "$(dirname "$ders_CONFIG_FILE")"
    touch "$ders_CONFIG_FILE"
fi

# Function to get menu title for managing configuration settings
ders_get_menu_title_manage_config() {
    echo "Manage Config Settings"
}

# Function to display configuration management menu
ders_show_config_menu() {
    clear
    echo "==============================="
    echo "  Manage Config Settings       "
    echo "==============================="
    echo "1. Server IP Address"
    echo "2. Reporting Hostname"
    echo "3. FB Auth Hostname"
    echo "4. Server Admin Email"
    echo "5. Back"
    echo "==============================="
    echo -n "Please choose an option [1-4]: "
}

# Function to handle configuration management
ders_run_script_manage_config() {
    while true; do
        ders_show_config_menu
        read ders_choice
        case $ders_choice in
            1)
                echo "Please enter the IP address for this server."
                ders_manage_server_ip_address
                ;;
            2)
                echo "Reporting Hostname selected."
                ders_manage_reporting_hostname
                ;;
            3)
                echo "FB Auth Hostname."
                ders_manage_fbauth_hostname
                ;;
            4)
                echo "Server Admin Email selected."
                ders_manage_server_admin_email
                ;;
            5)
                echo "Returning to main menu..."
                return
                ;;
            *)
                echo "Invalid option! Please choose a valid option."
                sleep 2
                ;;
        esac
    done
}
# Function to manage Server IP Address
ders_manage_server_ip_address() {
    ders_current_value=$(yq e '.server.ip_address' "$ders_CONFIG_FILE")
    echo "Current Setting for IP Address: $ders_current_value"
    echo -n "Enter new Server IP Address: "
    read ders_new_value
    yq e -i ".server.ip_address= \"$ders_new_value\"" "$ders_CONFIG_FILE"
    echo "Server IP Address updated to: $ders_new_value"
    sleep 1
}

# Function to manage Reporting Hostname
ders_manage_reporting_hostname() {
    ders_current_value=$(yq e '.reporting.hostname' "$ders_CONFIG_FILE")
    echo "Current Reporting Hostname: $ders_current_value"
    echo -n "Enter new Reporting Hostname: "
    read ders_new_value
    yq e -i ".reporting.hostname = \"$ders_new_value\"" "$ders_CONFIG_FILE"
    echo "Reporting Hostname updated to: $ders_new_value"
    sleep 1
}

# Function to manage FB Auth Hostname
ders_manage_fbauth_hostname() {
    ders_current_value=$(yq e '.fboauth.hostname' "$ders_CONFIG_FILE")
    echo "Current FB Auth Hostname: $ders_current_value"
    echo -n "Enter new FB Auth Hostname: "
    read ders_new_value
    yq e -i ".fboauth.hostname = \"$ders_new_value\"" "$ders_CONFIG_FILE"
    echo "FB Auth Hostname updated to: $ders_new_value"
    sleep 1
}

# Function to manage Server Admin Email
ders_manage_server_admin_email() {
    ders_current_value=$(yq e '.server.admin_email' "$ders_CONFIG_FILE")
    echo "Current Server Admin Email: $ders_current_value"
    echo -n "Enter new Server Admin Email: "
    read ders_new_value
    yq e -i ".server.admin_email = \"$ders_new_value\"" "$ders_CONFIG_FILE"
    echo "Server Admin Email updated to: $ders_new_value"
    sleep 1
}

# Function to check if the configuration management task is complete
ders_task_complete_manage_config() {
    return 0
}
