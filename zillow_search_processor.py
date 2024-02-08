import os
import json
from enum import Enum
from scraping_utility import *

class SortOrder(Enum):
    SORT_ORDER_ASCENDING = 1
    SORT_ORDER_DESCENDING = 2

SORT_ORDER = SortOrder.SORT_ORDER_DESCENDING
MAX_HOME_PRICE = 500000


def process_listings(directory):
    results = []

    # Iterate through all files in the directory
    for filename in os.listdir(directory):
        if filename.endswith(".json") and filename.startswith("listings_page"):
            file_path = os.path.join(directory, filename)

            # Read each JSON file
            with open(file_path, 'r', encoding='utf-8') as file:
                data = json.load(file)

                # Process each listing in the JSON file
                for listing in data:

                    listing_price = listing.get("unformattedPrice", 0)
                    Zestimate = listing.get("hdpData", {}).get("homeInfo", {}).get("zestimate", 0)
                    rentZestimate = listing.get("hdpData", {}).get("homeInfo", {}).get("rentZestimate", 0)
                    zip_code = int(listing.get("hdpData", {}).get("homeInfo", {}).get("zipcode", "00000"))
                    home_type = listing.get("hdpData", {}).get("homeInfo", {}).get("homeType", "NO_HOME_TYPE_PROVIDED")
                    area = listing.get("area", 0)
                    detailUrl = listing.get("detailUrl", "")

                    rentZestimate_to_area_ratio = rentZestimate / area if area != 0 else 0

                    # We filter out homes we do not want.
                    if listing_price == 0 or Zestimate == 0 or rentZestimate == 0 or home_type == "MANUFACTURED" or rentZestimate_to_area_ratio > 7.5 or detailUrl == "":
                        continue

                    rentZestimate_to_price_ratio = rentZestimate / listing_price if listing_price != 0 else 0
                    rentZestimate_to_Zestimate_ratio = rentZestimate / Zestimate if Zestimate != 0 else 0
                    results.append({
                        "zpid": listing.get("zpid"),
                        "address": listing.get("address", "NO_ADDRESS_PROVIDED"),
                        "home_type": home_type,
                        "zip_code": zip_code,
                        "url": detailUrl,
                        "rentZestimate_to_price_ratio": rentZestimate_to_price_ratio,
                        "rentZestimate_to_Zestimate_ratio": rentZestimate_to_Zestimate_ratio
                    })
    return results

def process_all_zip_codes(root_directory):
    all_results = []

    # Iterate through each subdirectory in the root directory
    for zip_code_folder in os.listdir(root_directory):
        folder_path = os.path.join(root_directory, zip_code_folder)

        # Check if it's a directory
        if os.path.isdir(folder_path):
            print(f"Processing listings in {folder_path}...")
            results = process_listings(folder_path)
            all_results.extend(results)

    reverse_sort = True if SORT_ORDER == SortOrder.SORT_ORDER_DESCENDING else False
    sorted_results = sorted(all_results, key=lambda x: x['rentZestimate_to_price_ratio'], reverse=reverse_sort)

    # Save all results to a new file
    with open(scrape_config['search_results_path'], 'w', encoding='utf-8') as outfile:
        json.dump(sorted_results, outfile, indent=4)


root_directory_path = 'HomeData'
process_all_zip_codes(root_directory_path)
