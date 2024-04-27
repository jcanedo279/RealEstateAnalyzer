import os
import json
import csv
from enum import Enum
from re_analyzer.utility.utility import SEARCH_LISTINGS_DATA_PATH, SEARCH_LISTINGS_METADATA_PATH, SEARCH_RESULTS_PROCESSED_PATH


class SortOrder(Enum):
    SORT_ORDER_ASCENDING = 1
    SORT_ORDER_DESCENDING = 2

SORT_ORDER = SortOrder.SORT_ORDER_DESCENDING
MAX_HOME_PRICE = 500000


def process_listings(search_listings_path, search_listings_metadata_path):
    results = []

    with open(search_listings_metadata_path, 'r') as search_listings_metadata_file:
        search_listings_metadata = json.load(search_listings_metadata_file)
        active_zpids_set = set(search_listings_metadata.get('active_zpids', {}))

    # Iterate through all files in the search_listings_path
    for filename in os.listdir(search_listings_path):
        if filename.endswith(".json") and filename.startswith("listings_"):
            file_path = os.path.join(search_listings_path, filename)

            # Read each JSON file
            with open(file_path, 'r', encoding='utf-8') as file:
                data = json.load(file)

                # Process each listing in the JSON file
                for listing in data:

                    listing_price = listing.get("unformattedPrice", 0)
                    Zestimate = listing.get("hdpData", {}).get("homeInfo", {}).get("zestimate", 0)
                    rentZestimate = listing.get("hdpData", {}).get("homeInfo", {}).get("rentZestimate", 0)
                    zpid = listing.get("zpid")
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
                        "zpid": zpid,
                        "address": listing.get("address", "NO_ADDRESS_PROVIDED"),
                        "home_type": home_type,
                        "zip_code": zip_code,
                        "url": detailUrl,
                        "listing_price": listing_price,
                        "restimate": rentZestimate,
                        "is_active": zpid in active_zpids_set,
                        "rentZestimate_to_price_ratio": rentZestimate_to_price_ratio,
                        "rentZestimate_to_Zestimate_ratio": rentZestimate_to_Zestimate_ratio
                    })
    return results

def process_all_zip_codes():
    all_results = []

    # Iterate through each subdirectory in the root directory
    for zip_code_folder in os.listdir(SEARCH_LISTINGS_DATA_PATH):
        search_listings_path = os.path.join(SEARCH_LISTINGS_DATA_PATH, zip_code_folder)
        search_listings_metadata_path = os.path.join(SEARCH_LISTINGS_METADATA_PATH, f'{zip_code_folder}_metadata.json')

        # Check if it's a directory
        if os.path.isdir(search_listings_path):
            print(f"Processing listings in {search_listings_path}...")
            results = process_listings(search_listings_path, search_listings_metadata_path)
            all_results.extend(results)

    reverse_sort = True if SORT_ORDER == SortOrder.SORT_ORDER_DESCENDING else False
    sorted_results = sorted(all_results, key=lambda x: x['rentZestimate_to_price_ratio'], reverse=reverse_sort)

    # Save all results to a new file
    with open(SEARCH_RESULTS_PROCESSED_PATH, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['zpid', 'address', 'home_type', 'zip_code', 'url', 'listing_price', 'restimate', 'is_active', 'rentZestimate_to_price_ratio', 'rentZestimate_to_Zestimate_ratio']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        writer.writeheader()
        for result in sorted_results:
            writer.writerow(result)


if __name__ == '__main__':
    process_all_zip_codes()
