import json
import hashlib

from zillowanalyzer.scrapers.scraping_utility import *

def generate_hash(value):
    """ Generate a hash for a given value """
    return hashlib.sha256(value.encode()).hexdigest()

def find_duplicates(search_results_path):
    with open(search_results_path, 'r', encoding='utf-8') as file:
        search_results = json.load(file)

    zpid_hashes = {}
    address_hashes = {}
    duplicates = {'zpid': [], 'address': []}

    for search_result in search_results:
        # Generate hashes for zpid and address
        zpid_hash = generate_hash(search_result["zpid"])
        address_hash = generate_hash(search_result["address"])

        # People mis-list their unit without the number so ignore these.
        if search_result["home_type"] in ["CONDO", "LOT"]:
            continue

        # Check for duplicate zpid
        if zpid_hash in zpid_hashes:
            duplicates['zpid'].append((zpid_hashes[zpid_hash], search_result["zpid"]))
        else:
            zpid_hashes[zpid_hash] = search_result["zpid"]

        # Check for duplicate address
        if address_hash in address_hashes:
            duplicates['address'].append((address_hashes[address_hash], search_result["address"]))
        else:
            address_hashes[address_hash] = search_result["address"]

    print(len(zpid_hashes), len(duplicates['zpid']))
    print(len(address_hashes), len(duplicates['address']))
    return duplicates

# Find duplicates
duplicates = find_duplicates(SEARCH_RESULTS_PATH)
# print("Duplicate ZPID found:", duplicates['zpid'])
# print("Duplicate addresses found:", duplicates['address'])
