import os
import math
import csv
import json
import random as rd
from datetime import datetime, timedelta

from zillowanalyzer.scrapers.scraping_utility import (
    retry_request, get_selenium_driver,
    extract_zestimate_history_from_driver, extract_property_details_from_driver
)
from zillowanalyzer.utility.utility import (
    PROJECT_CONFIG, DATA_PATH, PROPERTY_DETAILS_PATH,
    save_json, random_delay, ensure_directory_exists, batch_generator, is_within_cooldown_period
)
from zillowanalyzer.analyzers.iterator import get_property_info_from_property_details


PROPERTY_COOLDOWN_TIME_WINDOW = timedelta(days=2)


def should_extract_property_details(search_result):
    """
    Determines if the property details should be extracted based on various criteria.
    """
    zip_code, zpid = search_result['zip_code'], search_result['zpid']
    property_path = os.path.join(PROPERTY_DETAILS_PATH, zip_code, f'{zpid}_property_details.json')

    # 
    if not zip_code or not zpid:
        return True

    # Early return if the file does not exist or is empty.
    if not os.path.exists(property_path) or os.path.getsize(property_path) == 0:
        return True
    
    with open(property_path, 'r') as file:
        property_details = json.load(file)

    # Various conditions to decide if re-scraping is necessary.
    needs_update = (
        'props' not in property_details or
        property_details.get('last_checked', datetime.min) < datetime.now() - PROPERTY_COOLDOWN_TIME_WINDOW
    )
    return needs_update

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
            is_search_result_tracked = bool(search_result['is_active'])
            is_property_within_cooldown = is_within_cooldown_period(property_details.get('last_checked'), PROPERTY_COOLDOWN_TIME_WINDOW)
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
def extract_property_details_from_batch(batch, batch_ind, num_batches):
    # Start a driver context to scrape the unprocessed properties.
    with get_selenium_driver("about:blank") as driver:
        for search_result_ind, search_result in enumerate(batch):
            zip_code, zpid = search_result['zip_code'], search_result['zpid']
            property_url = search_result['url']

            print(f"Scraping property: {zpid} in zip_code: {zip_code} number: [{search_result_ind+1} / {len(batch)}] in batch: [{batch_ind+1} / {num_batches}] using profile: {PROJECT_CONFIG['profile_number']}", end='         \r')

            property_path = f'{PROPERTY_DETAILS_PATH}/{zip_code}/{zpid}_property_details.json'
            ensure_directory_exists('/'.join(property_path.split('/')[:-1]))
            
            # Navigate to the property's page and parse the HTML content.
            driver.get(property_url)
            zestimate_history = extract_zestimate_history_from_driver(driver)
            response = extract_property_details_from_driver(driver, 2)
            if not response:
                save_json({}, property_path)
                continue
            response['zestimateHistory'] = zestimate_history
            response['last_checked'] = datetime.now().isoformat()

            save_json(response, property_path)
            random_delay(3, 5)

# Roughly 5 seconds per response -> ~ 14 hours for 10,000 requests.
def extract_property_details_from_search_results(batch_size=5):
    csv_file_path = os.path.join(DATA_PATH, 'search_listings.csv')
    
    # Load and filter data.
    filtered_data = []
    with open(csv_file_path, newline='', encoding='utf-8') as csvfile:
        search_results = csv.DictReader(csvfile)
        filtered_data = [search_result for search_result in search_results if should_extract_property_details_from_search_results_safe(search_result)]
    rd.shuffle(filtered_data)
    
    # Calculate batches.
    total_filtered_rows = len(filtered_data)
    num_batches = math.ceil(total_filtered_rows / batch_size)

    # Generate and process batches.
    for batch_ind, batch in enumerate(batch_generator(filtered_data, batch_size)):
        search_results = [search_result for search_result in batch if should_extract_property_details_from_search_results_safe(search_result)]
        if not search_results:
            continue
        extract_property_details_from_batch(search_results, batch_ind, num_batches)

if __name__ == '__main__':
    extract_property_details_from_search_results()
