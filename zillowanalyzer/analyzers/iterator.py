import glob
import os
import json
import time
from datetime import timedelta

from zillowanalyzer.utility.utility import PROPERTY_DETAILS_PATH


def print_analysis_progress(start_time, analysis_index, analysis_len):
    current_time = time.time()
    elapsed_time = current_time - start_time
    # Calculate average time per property based on progress so far
    avg_time_per_property = elapsed_time / (analysis_index+1)
    # Estimate remaining time
    estimated_time_remaining = avg_time_per_property * (analysis_len - analysis_index)
    formatted_time_remaining = str(timedelta(seconds=int(estimated_time_remaining)))
    progress_percentage = 100 * (analysis_index+1) / analysis_len
    print(f"Analysing property [{analysis_index+1} | {analysis_len}]. {progress_percentage:.2f}% analyzed, time remaining: ~{formatted_time_remaining}", end=' '*30 + '\r')

def property_details_iterator():
    pattern = os.path.join(PROPERTY_DETAILS_PATH, "*", "*_property_details.json")
    property_detail_files = glob.glob(pattern)
    num_properties = len(property_detail_files)

    start_time = time.time()

    # Use glob to find all matching files
    for json_file_index, json_file_path in enumerate(property_detail_files):
        print_analysis_progress(start_time, json_file_index, num_properties)
        # Open and load JSON data from the file
        with open(json_file_path, 'r') as json_file:
            property_details = json.load(json_file)
            # Yield the loaded JSON data
            if 'props' not in property_details:
                continue
            yield property_details

def get_property_info_from_property_details(property_details):
    property_data = property_details['props']['pageProps']['componentProps']
    if 'gdp' in property_data:
        property_data = property_data['gdp']
    elif 'gdpClientCache' in property_data:
        property_data = property_data['gdpClientCache']
    else:
        return None
    first_key = next(iter(property_data))
    property_info = property_data[first_key].get("property", None)
    
    if not property_info:
        return None
    return property_info
