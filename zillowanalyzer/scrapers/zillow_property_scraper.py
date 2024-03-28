import os
import math
import csv
import json
import time
import random as rd

from datetime import datetime, timedelta

from zillowanalyzer.scrapers.scraping_utility import retry_request, get_selenium_driver, extract_zestimate_history_from_driver, extract_property_details_from_driver
from zillowanalyzer.utility.utility import PROJECT_CONFIG, DATA_PATH, PROPERTY_DETAILS_PATH
from zillowanalyzer.utility.utility import save_json, random_delay, ensure_directory_exists, is_last_checked_string_within_search_cooldown
from zillowanalyzer.analyzers.iterator import get_property_info_from_property_details


PROPERTY_COOLDOWN_TIME_WINDOW = timedelta(days=2)


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

def should_extract_property_details_from_search_results_safe(search_result):
    zip_code, zpid = search_result['zip_code'], search_result['zpid']
    property_path = f'{PROPERTY_DETAILS_PATH}/{zip_code}/{zpid}_property_details.json'
    if not os.path.exists(property_path) or os.path.getsize(property_path) == 0:
        return True
    return False

def should_extract_property_details_from_search_results(search_result):
    zip_code, zpid = search_result['zip_code'], search_result['zpid']
    property_path = f'{PROPERTY_DETAILS_PATH}/{zip_code}/{zpid}_property_details.json'
    if os.path.exists(property_path) and (search_result['listing_price'] or search_result['restimate']):
        if os.path.getsize(property_path) == 0:
            # We need to extract if existing property data is missing.
            return True
        with open(property_path, 'r') as property_path_file:
            property_details = json.load(property_path_file)
            if 'props' not in property_details:
                # We need to extract if existing property_details is not filled completely.
                return True
            property_info = get_property_info_from_property_details(property_details)
            if not property_info:
                return True
            is_search_result_tracked = bool(search_result['is_tracked'])
            is_property_within_cooldown = is_last_checked_string_within_search_cooldown(property_details.get('last_checked'), PROPERTY_COOLDOWN_TIME_WINDOW)
            is_price_outside_threshold = abs(property_info['price'] - int(search_result['listing_price'])) >= 0.05 * property_info['price']
            restimate = property_info.get('rentZestimate', 0)
            if not restimate:
                restimate = 0
            is_restimate_outside_threshold = abs(restimate - int(search_result['restimate'])) >= 0.05 * restimate
            # We should re-try the property scrape if the search result is still being tracked in the search scraper
            # and either the search results price or restimate are different enough from the existing property data.
            return (is_search_result_tracked and not is_property_within_cooldown) and (is_price_outside_threshold or is_restimate_outside_threshold)
    # We need to extract if property_path DNE.
    return True

@retry_request(PROJECT_CONFIG)
def extract_property_details_from_batch(property_data, batch_ind, batch_size, num_batches):
    # Start a driver context to scrape the unprocessed properties.
    with get_selenium_driver("about:blank") as driver:
        for search_result_ind, search_result in enumerate(property_data):
            zip_code, zpid = search_result['zip_code'], search_result['zpid']
            property_url = '/'.join(search_result['url'].replace('homedetails', 'homes').split('/')[:-2]).replace('zpid', 'rb')
            print(property_url)

            num_search_results_digits = len(str(batch_size))
            num_batches_digits = len(str(num_batches))
            formatted_search_result_ind = f"{search_result_ind+1:0{num_search_results_digits}d}"
            formatted_batch_ind = f"{batch_ind+1:0{num_batches_digits}d}"
            print(f'Scraping property: {zpid} in zip_code: {zip_code} number: [{formatted_search_result_ind} / {batch_size}] in batch: [{formatted_batch_ind} / {num_batches}]', end='         \r')

            property_path = f'{PROPERTY_DETAILS_PATH}/{zip_code}/{zpid}_property_details.json'
            ensure_directory_exists('/'.join(property_path.split('/')[:-1]))
            
            # Navigate to the property's page and parse the HTML content.
            driver.get(property_url)
            zestimate_history = extract_zestimate_history_from_driver(driver, 2)
            response = extract_property_details_from_driver(driver, 1)
            if not response:
                save_json({}, property_path)
                continue
            response['zestimateHistory'] = zestimate_history
            response['last_checked'] = datetime.now().isoformat()

            save_json(response, property_path)
            random_delay(2, 4)

# Roughly 5 seconds per response -> ~ 14 hours for 10,000 requests.
def extract_property_details_from_search_results(batch_size=5):
    csv_file_path = os.path.join(DATA_PATH, 'search_listings.csv')

    reader_data = []
    with open(csv_file_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        reader_data = [row for row in reader]
    # rd.shuffle(reader_data)

    total_rows = sum(1 for _ in open(csv_file_path)) - 1
    num_batches = math.ceil(total_rows / batch_size)

    reader_gen = (row for row in reader_data)
    for batch_ind, batch in enumerate(batch_generator(reader_gen, batch_size)):
        # Loop through the batch and make sure that the properties have not already been processed.
        property_data = []
        for search_result in batch:
            if not should_extract_property_details_from_search_results(search_result):
                continue
            # zip_code, zpid = search_result['zip_code'], search_result['zpid']
            # property_path = f'{PROPERTY_DETAILS_PATH}/{zip_code}/{zpid}_property_details.json'
            # if os.path.exists(property_path):
            #     continue
            property_data.append(search_result)
        if not property_data:
            continue
        extract_property_details_from_batch(property_data, batch_ind, batch_size, num_batches)


extract_property_details_from_search_results()
