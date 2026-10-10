# config.py

menu_config = {
    "main_menu": {
        "title": "Main Menu",
        "options": [
            {"key": "1", "text": "Company Assets", "next_menu": "company_assets"},
            {"key": "2", "text": "Facebook Assets", "next_menu": "facebook_assets"},
            {"key": "3", "text": "Google Ads Assets", "next_menu": "google_ads_assets"},
            {"key": "4", "text": "Echo Assets", "next_menu": "echo_assets"},
            {"key": "5", "text": "Tools", "next_menu": "tools_menu"},
            {"key": "6", "text": "Exit", "next_menu": "exit"},
        ],
    },
    "company_assets": {
        "title": "Company Assets",
        "options": [
            {
                "key": "1",
                "text": "Teams",
                "next_menu": "crud_menu",
                "item": "Team",
                "handler": "teams",
            },
            {
                "key": "2",
                "text": "Regions",
                "next_menu": "crud_menu",
                "item": "Region",
                "handler": "regions",
            },
            {
                "key": "3",
                "text": "Countries",
                "next_menu": "crud_menu",
                "item": "Country",
                "handler": "countries",
            },
            {
                "key": "4",
                "text": "Cities",
                "next_menu": "crud_menu",
                "item": "City",
                "handler": "cities",
            },
            {"key": "5", "text": "Back", "next_menu": "previous"},
        ],
    },
    "echo_assets": {
        "title": "Echo Assets",
        "options": [
            {
                "key": "1",
                "text": "Tenants",
                "next_menu": "crud_menu",
                "item": "Tenant",
                "handler": "echo_tenants",
            },
            {"key": "5", "text": "Back", "next_menu": "previous"},
        ],
    },
    "facebook_assets": {
        "title": "Facebook Assets",
        "options": [
            {
                "key": "1",
                "text": "Facebook Accounts",
                "next_menu": "crud_menu",
                "item": "Facebook Account",
                "handler": "facebook_accounts",
            },
            {
                "key": "2",
                "text": "Facebook Ad Accounts",
                "next_menu": "crud_menu",
                "item": "Facebook Ad Account",
                "handler": "facebook_ad_accounts",
            },
            {
                "key": "3",
                "text": "Facebook Apps",
                "next_menu": "crud_menu",
                "item": "Facebook App",
                "handler": "facebook_apps",
            },
            {
                "key": "3",
                "text": "Facebook Users",
                "next_menu": "crud_menu",
                "item": "Facebook Users",
                "handler": "facebook_app_users",
            },
            {
                "key": "4",
                "text": "Facebook Pages",
                "next_menu": "crud_menu",
                "item": "Facebook Page",
                "handler": "facebook_pages",
            },
            {"key": "5", "text": "Back", "next_menu": "previous"},
        ],
    },
    "google_ads_assets": {
        "title": "Google Ads Assets",
        "options": [
            {
                "key": "1",
                "text": "Google Ads Customers",
                "next_menu": "crud_menu",
                "item": "Google Ads Customer",
                "handler": "google_ads_customers",
            },
            {
                "key": "2",
                "text": "Google Analytics Accounts",
                "next_menu": "crud_menu",
                "item": "Google Analytics Account",
                "handler": "ga_accounts",
            },
            {
                "key": "3",
                "text": "Google Analytics Properties",
                "next_menu": "crud_menu",
                "item": "Google Analytics Property",
                "handler": "ga_properties",
            },
            {"key": "4", "text": "Back", "next_menu": "previous"},
        ],
    },
    "tools_menu": {
        "title": "Tools",
        "options": [
            {
                "key": "1",
                "text": "Fix unknown Echo sources",
                "action": "main",
                "handler": "tools.fix_unknown_echo_sources",
            },
            {"key": "2", "text": "Back", "next_menu": "previous"},
        ],
    },
    "crud_menu": {
        "title": "Manage {item}",
        "options": [
            {"key": "1", "text": "Add New {item}", "action": "add", "item": "{item}"},
            {"key": "2", "text": "View {item}", "action": "view", "item": "{item}"},
            {"key": "3", "text": "Back", "next_menu": "previous"},
        ],
    },
}
