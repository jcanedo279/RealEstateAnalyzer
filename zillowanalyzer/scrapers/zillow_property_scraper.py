import os
import math
import csv
import json
import pandas as pd
from bs4 import BeautifulSoup
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

from datetime import datetime, timedelta

from zillowanalyzer.scrapers.scraping_utility import (
    retry_request, get_selenium_driver, offscreen_click
)
from zillowanalyzer.utility.utility import (
    PROJECT_CONFIG, SEARCH_RESULTS_PROCESSED_PATH, PROPERTY_DETAILS_PATH,
    save_json, random_delay, ensure_directory_exists, is_within_cooldown_period, batch_generator, parse_dates
)
from zillowanalyzer.analyzers.iterator import get_property_info_from_property_details


PROPERTY_COOLDOWN_TIME_WINDOW = timedelta(days=2)


def should_extract_property_details(search_result):
    """
        Determines if the property details should be extracted based on various criteria.
    """
    zip_code, zpid = search_result['zip_code'], search_result['zpid']
    property_path = os.path.join(PROPERTY_DETAILS_PATH, zip_code, f'{zpid}_property_details.json')

    if not zip_code or not zpid:
        # If no zip_code or zpid were found in the search_results, this property is corrupted -> don't pull.
        return False

    if not os.path.exists(property_path) or os.path.getsize(property_path) == 0:
        # Re-Extract if the existing property_details is corrupted.
        return True
    
    with open(property_path, 'r') as file:
        property_details = json.load(file)
    if 'props' not in property_details:
        # Extract if the existing property_details is not filled correctly.
        return True
    
    property_info = get_property_info_from_property_details(property_details)
    if not property_info:
        # Re-Extract if the existing property_details is corrupted.
        return True
    restimate, price = property_info.get('rentZestimate', 0), property_info.get('price', 0)
    if not restimate:
        restimate = 0

    needs_update = (
        (
            # Extract if the search_result is an active listing, given that its outside the cooldown period.
            not is_within_cooldown_period(property_details.get('last_checked'), PROPERTY_COOLDOWN_TIME_WINDOW) and
            bool(search_result['is_active'])
        ) and (
            # Extract if the existing property_details is significantly (5%) different than the new search_results.
            abs(price - int(search_result['listing_price'])) >= 0.05 * price or
            abs(restimate - int(search_result['restimate'])) >= 0.05 * restimate
        )
    )
    return needs_update


# Roughly 5 seconds per response -> ~ 14 hours for 10,000 requests.
def extract_property_details_from_search_results(batch_size=5):
    """
        Main function to initiate the property details extraction process.
    """
    
    # Load and filter data.
    search_results = []
    with open(SEARCH_RESULTS_PROCESSED_PATH, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        search_results = [search_result for search_result in reader if should_extract_property_details(search_result)]

    # Generate and process batches.
    num_batches = math.ceil(len(search_results) / batch_size)
    for batch_ind, batch in enumerate(batch_generator(search_results, batch_size)):
        extract_property_details_from_batch(batch, batch_ind, batch_size, num_batches)


@retry_request(PROJECT_CONFIG)
def extract_property_details_from_batch(property_data, batch_ind, batch_size, num_batches):
    """
        Processes and saves property details for a batch of search results.
    """
    with get_selenium_driver("about:blank") as driver:
        for search_result_ind, search_result in enumerate(property_data):
            zip_code, zpid = search_result['zip_code'], search_result['zpid']
            print(f'Scraping property: {zpid} in zip_code: {zip_code} number: [{search_result_ind} / {batch_size}] in batch: [{batch_ind} / {num_batches}]', end='         \r')

            property_path = f'{PROPERTY_DETAILS_PATH}/{zip_code}/{zpid}_property_details.json'
            ensure_directory_exists('/'.join(property_path.split('/')[:-1]))
            
            # Navigate to the property's page and parse the HTML content.
            response = extract_property_details_from_driver(driver, search_result['url'], 1)
            if not response:
                save_json({}, property_path)
                continue
            response['zestimateHistory'] = extract_zestimate_history_from_driver(driver)
            response['last_checked'] = datetime.now().isoformat()

            save_json(response, property_path)
            random_delay(2, 4)


@retry_request(PROJECT_CONFIG)
def extract_property_details_from_driver(driver, property_url, delay):
    """
        Navigates to and extracts property details from the WebDriver.
    """
    driver.get(property_url)
    random_delay(delay, 2*delay)
    soup = BeautifulSoup(driver.page_source, 'html.parser')

    # Find the input or script tag
    script_tag = soup.find('script', {'id': '__NEXT_DATA__'})

    # Extract JSON from the appropriate tag
    if script_tag:
        json_data = json.loads(script_tag.string)
    else:
        return None
    
    # Check and parse 'gdpClientCache'
    gdp_client_cache = json_data['props']['pageProps']['componentProps'].get('gdpClientCache')
    if isinstance(gdp_client_cache, str):
        json_data['props']['pageProps']['componentProps']['gdpClientCache'] = json.loads(gdp_client_cache)

    return json_data


def extract_zestimate_history_from_driver(driver):
    """
        Extracts Zestimate history from a Zillow property page using a driver.

        This function navigates through the property page to find and click on the "Table view"
        button within the Zestimate section to reveal the Zestimate history table. It then extracts
        the Zestimate values and their corresponding dates from the table.
    """
    try:
        container = driver.find_element(By.ID, "ds-home-values")
    except NoSuchElementException:
        # There is no Zestimate history for this home (likely its off market).
        return []

    table_view_button = None
    # We check if the zestimate history button is either directly in our text or in a child's text.
    try:
        table_view_button = container.find_element(By.XPATH, "//button[.//text()[contains(., 'Table view')] or contains(., 'Table view')]")
    except:
        pass
    # If the button to expand the table is not visible, we need to expand the Zestiamte section first.
    if not table_view_button or not table_view_button.is_displayed():
        try:
            show_more_button = container.find_element(By.XPATH, ".//button[contains(., 'Show more')]")
            offscreen_click(show_more_button, driver)
            table_view_button = container.find_element(By.XPATH, ".//button[contains(text(), 'Table view')]")
        except NoSuchElementException:
            pass
    # If no data is available in the Zestimate, early return to avoid suspicious behavior.
    try:
        empty_zestimate_history_element = driver.find_element(By.XPATH, "//strong[contains(text(), 'No data available at this time.')]")
        if empty_zestimate_history_element.is_displayed():
            return []
    except NoSuchElementException:
        pass
    offscreen_click(table_view_button, driver)
    
    # Wait for the element to be loaded.
    zestimate_history_selector = '//table[@data-testid="zestimate-history"]'
    try:
        table_element = driver.find_element(By.XPATH, zestimate_history_selector)
    except NoSuchElementException:
        return []
    # Now, you can either directly extract the text, or further navigate to rows and cells as needed
    table_html = table_element.get_attribute('outerHTML')
    
    soup = BeautifulSoup(table_html, 'html.parser')
    table = soup.find('table', {'data-testid': 'zestimate-history'})

    zestimate_history = []
    for row in table.find('tbody').find_all('tr'):
        cells = row.find_all('td')
        date, price = cells[0].text, cells[1].text
        zestimate_history.append({
            "Date": date,
            "Price": price
        })
    if zestimate_history == []:
        return zestimate_history
    
    zestimate_history_df = pd.DataFrame(zestimate_history)
    zestimate_history_df["Price"] = zestimate_history_df["Price"].str.replace(r'[^\d.]+', '', regex=True).astype(float) * 1000
    zestimate_history_df["Date"] = zestimate_history_df["Date"].apply(parse_dates)
    zestimate_history_df["Date"] = zestimate_history_df["Date"].apply(lambda x: x.strftime('%Y-%m-%dT%H:%M:%S'))
    return zestimate_history_df.to_dict('records')


if __name__ == '__main__':
    extract_property_details_from_search_results()
