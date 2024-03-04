import os
import json
import re
import shutil
import glob
from collections import defaultdict

from zillowanalyzer.scrapers.zillow_search_scraper import maybe_save_current_search_results
from zillowanalyzer.utility.utility import load_json, DATA_PATH, SEARCH_LISTINGS_METADATA_PATH

base_dir = "zillowanalyzer/Data/SearchResults/"

municipality_to_zpids = defaultdict(set)
def load_all_metadata():
    for file_path in glob.glob(os.path.join(SEARCH_LISTINGS_METADATA_PATH, "*_metadata.json")):
        municipality = os.path.basename(file_path).split("_metadata.json")[0]
        print( municipality, file_path )
        
        with open(file_path, "r") as file:
            municipality_to_zpids[municipality].update(json.load(file)['zpids'])

municipality_to_zip_codes = load_json(f"{DATA_PATH}/florida_overflowing_municipalities_to_zip_codes.json")
MASTER_ZPID_SET = {zpid for zpid_set in municipality_to_zpids.values() for zpid in zpid_set}
load_all_metadata()


def is_zip_code(directory_name):
    # Check if the directory name is a 5-digit code, indicating it's a zip code
    return re.match(r"^\d{5}$", directory_name) is not None

def migrate_listings():
    city_to_listings = defaultdict(list)
    # Iterate over all directories in the base directory
    for dir_name in os.listdir(base_dir):
        zip_code_path = os.path.join(base_dir, dir_name)
        if not os.path.isdir(zip_code_path) or not is_zip_code(dir_name):
            continue  # Skip files and non-zip-code directories

        for file_name in os.listdir(zip_code_path):
            file_path = os.path.join(zip_code_path, file_name)
            with open(file_path, 'r') as file:
                listings = json.load(file)

            for listing in listings:
                city = listing.get('addressCity')
                if city:
                    city_to_listings[city].append(listing)
    print(city_to_listings.keys())
    important_cities = ['Palm Bay', 'Fort Lauderdale', 'Panama City Beach', 'Panama City']
    for city in important_cities:
        important_listings = [listing for listing in city_to_listings[city] if listing['zpid'] not in MASTER_ZPID_SET]
        print(city, len(important_listings))
        # maybe_save_current_search_results(city, important_listings)

if __name__ == "__main__":
    migrate_listings()
