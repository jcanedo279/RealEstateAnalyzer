import sys
import math
from scraping_utility import *

def delete_file(file_path):
    os.remove(file_path)
    print(f"Deleted {file_path}")
def delete_null_content_files(directory):
    for subdir, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(subdir, file)
            if file.endswith('.DS_Store'):
                delete_file(file_path)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content == 'null' or not content:
                        delete_file(file_path)
            except Exception as e:
                print(f"Error reading file {file_path}: {e}")

def batch_generator(data, batch_size):
    """Yield successive n-sized chunks from data."""
    for i in range(0, len(data), batch_size):
        yield data[i:i + batch_size]

# Roughly 5 seconds per response -> ~ 14 hours for 10,000 requests.
def extract_property_details_from_search_results(batch_size=5):
    search_results = load_json(scrape_config['search_results_path'])

    num_batches = math.ceil(len(search_results) / batch_size)
    for batch_ind, batch in enumerate(batch_generator(search_results, batch_size)):
        
        # Loop through the batch and make sure that the properties have not already been processed.
        property_data = []
        for search_result_ind, search_result in enumerate(batch):
            zip_code, zpid = search_result['zip_code'], search_result['zpid']
            property_path = f'PropertyDetails/{zip_code}/{zpid}_property_details.json'
            if os.path.exists(property_path):
                continue
            property_data.append(search_result)
        if not property_data:
            continue

        # Start a driver context to scrape the unprocessed properties.
        with get_selenium_driver("about:blank") as driver:
            for search_result_ind, search_result in enumerate(property_data):
                zip_code, zpid = search_result['zip_code'], search_result['zpid']
                property_url = search_result['url']

                num_search_results_digits = len(str(len(search_results)))
                num_batches_digits = len(str(num_batches))
                formatted_search_result_ind = f"{search_result_ind+1:0{num_search_results_digits}d}"
                formatted_batch_ind = f"{batch_ind+1:0{num_batches_digits}d}"
                print(f'Scraping property: {zpid} in zip_code: {zip_code} number: [{formatted_search_result_ind} / {batch_size}] in batch: [{formatted_batch_ind} / {num_batches}]', end='         \r')

                property_path = f'PropertyDetails/{zip_code}/{zpid}_property_details.json'
                if os.path.exists(property_path):
                    continue
                ensure_directory_exists('/'.join(property_path.split('/')[:-1]))
                
                # Navigate to the property's page and parse the HTML content.
                driver.get(property_url)
                zestimate_history = extract_zestimate_history_from_driver(driver, scrape_config['2s_delay'])
                response = extract_property_details_from_driver(driver, scrape_config['2s_delay'])
                response['zestimateHistory'] = zestimate_history

                save_json(response, property_path)
                random_delay(scrape_config['3s_delay'], scrape_config['5s_delay'])


# First we delete files which were not properly filled (likely due to connection and security).
# base_directory = "PropertyDetails"
# delete_null_content_files(base_directory)

extract_property_details_from_search_results()
# with get_selenium_driver("https://bot.sannysoft.com/") as driver:
#     time.sleep(1000)
