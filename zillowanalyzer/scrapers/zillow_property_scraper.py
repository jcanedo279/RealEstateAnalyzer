import math
import csv
from zillowanalyzer.scrapers.scraping_utility import *


def batch_generator(data, batch_size):
    """A generator to yield batches of data."""
    batch = []
    for item in data:
        batch.append(item)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch

@retry_request(scrape_config)
def extract_property_details_from_batch(property_data, batch_ind, batch_size, num_batches):
    # Start a driver context to scrape the unprocessed properties.
    with get_selenium_driver("about:blank") as driver:
        for search_result_ind, search_result in enumerate(property_data):
            zip_code, zpid = search_result['zip_code'], search_result['zpid']
            property_url = search_result['url']

            num_search_results_digits = len(str(batch_size))
            num_batches_digits = len(str(num_batches))
            formatted_search_result_ind = f"{search_result_ind+1:0{num_search_results_digits}d}"
            formatted_batch_ind = f"{batch_ind+1:0{num_batches_digits}d}"
            print(f'Scraping property: {zpid} in zip_code: {zip_code} number: [{formatted_search_result_ind} / {batch_size}] in batch: [{formatted_batch_ind} / {num_batches}]', end='         \r')

            property_path = f'{PROPERTY_DETAILS_PATH}/{zip_code}/{zpid}_property_details.json'
            if os.path.exists(property_path):
                continue
            ensure_directory_exists('/'.join(property_path.split('/')[:-1]))
            
            # Navigate to the property's page and parse the HTML content.
            driver.get(property_url)
            zestimate_history = extract_zestimate_history_from_driver(driver, scrape_config['2s_delay'])
            response = extract_property_details_from_driver(driver, 1)
            if not response:
                save_json({}, property_path)
                continue
            response['zestimateHistory'] = zestimate_history

            save_json(response, property_path)
            random_delay(2, 4)

# Roughly 5 seconds per response -> ~ 14 hours for 10,000 requests.
def extract_property_details_from_search_results(batch_size=5):
    csv_file_path = os.path.join(DATA_PATH, 'search_listings.csv')

    reader_data = []
    with open(csv_file_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        reader_data = [row for row in reader]

    reader_gen = (row for row in reader_data)


    total_rows = sum(1 for _ in open(csv_file_path)) - 1
    num_batches = math.ceil(total_rows / batch_size)
    for batch_ind, batch in enumerate(batch_generator(reader_gen, batch_size)):
        
        # Loop through the batch and make sure that the properties have not already been processed.
        property_data = []
        for search_result in batch:
            zip_code, zpid = search_result['zip_code'], search_result['zpid']
            property_path = f'{PROPERTY_DETAILS_PATH}/{zip_code}/{zpid}_property_details.json'
            if os.path.exists(property_path):
                continue
            property_data.append(search_result)
        if not property_data:
            continue

        extract_property_details_from_batch(property_data, batch_ind, batch_size, num_batches)

        


extract_property_details_from_search_results()
# with get_selenium_driver("https://bot.sannysoft.com/") as driver:
#     time.sleep(1000)
