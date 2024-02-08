import json
import hashlib

def generate_hash(value):
    """ Generate a hash for a given value """
    return hashlib.sha256(value.encode()).hexdigest()

def find_duplicates(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        listings = json.load(file)

    zpid_hashes = {}
    address_hashes = {}
    duplicates = {'zpid': [], 'address': []}

    for listing in listings:
        # Generate hashes for zpid and address
        zpid_hash = generate_hash(listing["zpid"])
        address_hash = generate_hash(listing["address"])

        # People mis-list their unit without the number so ignore these.
        if listing["home_type"] in ["CONDO", "LOT"]:
            continue

        # Check for duplicate zpid
        if zpid_hash in zpid_hashes:
            duplicates['zpid'].append((zpid_hashes[zpid_hash], listing["zpid"]))
        else:
            zpid_hashes[zpid_hash] = listing["zpid"]

        # Check for duplicate address
        if address_hash in address_hashes:
            duplicates['address'].append((address_hashes[address_hash], listing["address"]))
        else:
            address_hashes[address_hash] = listing["address"]

    print(len(zpid_hashes), len(duplicates['zpid']))
    print(len(address_hashes), len(duplicates['address']))
    return duplicates

# Path to the JSON file
file_path = 'calculated_ratios_all_zip_codes.json'

# Find duplicates
duplicates = find_duplicates(file_path)
# print("Duplicate ZPID found:", duplicates['zpid'])
# print("Duplicate addresses found:", duplicates['address'])
