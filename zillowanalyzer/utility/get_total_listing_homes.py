import os
import json
from glob import glob

from zillowanalyzer.utility.utility import DATA_PATH, SEARCH_LISTINGS_METADATA_PATH

# Assuming the base directory for listing data files
listings_path = os.path.join(SEARCH_LISTINGS_METADATA_PATH, '**', '*.json')
listing_files = glob(listings_path, recursive=True)

unique_zpids = set()
overflowing_municipalities = set()
municipality_to_num_zpids = {}


# Iterate over each file path in the listing files
for file_path in listing_files:
    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
        municipality_to_num_zpids[file_path.split('/')[3]] = len(data['zpids'])
        for zpid in data['zpids']:
            unique_zpids.add(zpid)

# Calculate total unique zpids
total_unique_zpids = len(unique_zpids)

print(total_unique_zpids)

sorted_municipality_to_num_zpids = {k: v for k, v in sorted(municipality_to_num_zpids.items(), key=lambda item: item[1]) if v > 770}
print(sorted_municipality_to_num_zpids)
with open(os.path.join(DATA_PATH, "florida_municipalities_overflowing.txt"), 'w') as f:
    f.write( '\n'.join(sorted_municipality_to_num_zpids.keys()) )
