from lib.es_helpers import add_document


def run_update():
    """This function defines the update process."""
    print("Running Elasticsearch update script for version 2...")

    # Regions to be added
    regions = [
        {"region_title": "Euro", "region_description": "Europe"},
        {
            "region_title": "NAME",
            "region_description": "North Africa and the Middle East",
        },
        {"region_title": "SSA", "region_description": "Sub-Saharan Africa"},
        {"region_title": "CA", "region_description": "Central Asia"},
        {"region_title": "SA", "region_description": "South Asia"},
        {"region_title": "APAC", "region_description": "Asia Pacific"},
        {"region_title": "AMP", "region_description": "American Peoples"},
    ]

    # Elasticsearch index for regions
    index_name = "de_asset_regions"

    # Add each region to Elasticsearch
    for region in regions:
        print(f"Adding Region: {region['region_title']}")
        if add_document(index_name, region):
            print(f"Region '{region['region_title']}' added successfully.")
        else:
            print(f"Failed to add Region: {region['region_title']}")

    print("Region update process completed.")
    return "success"  # Return 'success' to indicate that the update was applied
