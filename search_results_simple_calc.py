import os
import json
from glob import glob

# Assuming the base directory for listing data files
base_directory = 'zillowanalyzer/Data/SearchResults/'
listing_files = glob(base_directory + '**/*.json', recursive=True)

total_listings = 0
total_photos = 0
unique_zpids = set()

# Iterate over each file path in the listing files
for file_path in listing_files:
    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)
        for listing in data:
            zpid = listing.get('zpid')
            if zpid:
                unique_zpids.add(zpid)
            photos = listing.get('carouselPhotos', [])
            total_photos += len(photos)
        total_listings += len(data)

# Calculate total unique zpids
total_unique_zpids = len(unique_zpids)

print(total_listings, total_photos, total_unique_zpids)

