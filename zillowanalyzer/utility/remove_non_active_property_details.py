import pandas as pd
import os

from zillowanalyzer.utility.utility import SEARCH_RESULTS_PROCESSED_PATH, PROPERTY_DETAILS_PATH

df = pd.read_csv(SEARCH_RESULTS_PROCESSED_PATH)
# Create a dictionary for faster lookup, mapping zpid to is_active status
active_status_dict = pd.Series(df.is_active.values, index=df.zpid).to_dict()

# Iterate over the property details directory
total_deleted = 0
for root, dirs, files in os.walk(PROPERTY_DETAILS_PATH):
    for file in files:
        print(file)
        if file.endswith("_property_details.json"):
            # Extract the zpid from the file name
            zpid = int(file.split('_')[0])
            is_active = active_status_dict.get(zpid, False)

            if not is_active:
                file_path = os.path.join(root, file)
                os.remove(file_path)
                print(f"Deleted {file_path}")
                total_deleted += 1
print(f"Deleted {total_deleted} files.")
