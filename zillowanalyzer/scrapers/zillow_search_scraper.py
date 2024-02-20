import os
import json
import glob
import re
from collections import defaultdict
from datetime import datetime, timedelta

from zillowanalyzer.scrapers.scraping_utility import *


SEARCH_COOLDOWN_TIME_WINDOW = timedelta(days=1)
profile_search_setup_done = {profile_number:False for profile_number in range(scrape_config['min_profile_number'], scrape_config['max_profile_number']+1)}

def load_all_municipalities():
    file_path = f'{DATA_PATH}/florida_municipalities_data.txt'
    # A dictionary to hold the mapping of counties to their municipalities
    county_to_municipalities = defaultdict(list)

    with open(file_path, 'r') as file:
        for line in file:
            # Using regex to split the line by tabs or multiple spaces
            fields = re.split(r'\t+', line.strip())
            
            # Adjust the index based on the actual structure if needed
            municipality = fields[1].strip()
            county = fields[2].strip()

            # Remove the † symbol if present
            municipality_cleaned = municipality.replace("†", "")
            county_to_municipalities[county].append(municipality_cleaned)
    return county_to_municipalities

def load_all_metadata():
    for file_path in glob.glob(os.path.join(SEARCH_RESULTS_METADATA_PATH, "*_metadata.json")):
        municipality = os.path.basename(file_path).split("_metadata.json")[0]
        
        with open(file_path, "r") as file:
            municipality_to_zpids[municipality].update(json.load(file)['zpids'])

county_to_municipalities = load_all_municipalities()
municipality_to_zpids = defaultdict(set)
ensure_directory_exists(SEARCH_RESULTS_METADATA_PATH)
load_all_metadata()
MASTER_ZPID_SET = {zpid for zpid_set in municipality_to_zpids.values() for zpid in zpid_set}
all_municipalities = [municipality for county in county_to_municipalities.keys() for municipality in county_to_municipalities[county]]

def should_process_municipality(municipality):
    search_results_metadata_path = f'{SEARCH_RESULTS_METADATA_PATH}/{municipality}_metadata.json'
    if not os.path.exists(search_results_metadata_path):
        return True
    search_results_metadta = load_json(search_results_metadata_path)
    last_checked_str = search_results_metadta.get('last_checked')
    if not last_checked_str:
        return True
    last_checked = datetime.fromisoformat(last_checked_str)
    if datetime.now() - last_checked < SEARCH_COOLDOWN_TIME_WINDOW:
        return False
    return True

def maybe_click_next_page(driver, current_page, skip_scrolling=False):
    try:
        next_page_link = driver.find_element(By.CSS_SELECTOR, f'a[title="Page {current_page+1}"]')
        if not skip_scrolling:
            scroll_to_element(driver, "#search-page-list-container", ".search-pagination")
        if next_page_link:
            offscreen_click(next_page_link, driver)
            random_delay(scrape_config['1s_delay'], scrape_config['2s_delay'])
        else:
            return False
    except NoSuchElementException:
        return False
    except TimeoutException:
        return False
    return True

def maybe_save_new_search_results(municipality, search_results, new_zpids_in_municipality, saved_zpids_in_municipality):
    municipality_path = f"{SEARCH_RESULTS_DATA_PATH}/{municipality}"
    new_search_results = [search_result for search_result in search_results if search_result['zpid'] in new_zpids_in_municipality]
    # We save even if new_search_results is empty to ensure the metadata exists to skip over this municipality.
    current_datetime = datetime.now()
    ensure_directory_exists(municipality_path)
    save_json(new_search_results, f'{municipality_path}/listings_{current_datetime.strftime("%Y-%m-%d_%H-%M")}.json')
    saved_zpids_in_municipality.update(new_zpids_in_municipality)

    search_metadata = {
        'zpids': list(saved_zpids_in_municipality),
        'last_checked': current_datetime.isoformat()
    }
    save_json(search_metadata, f'{SEARCH_RESULTS_METADATA_PATH}/{municipality}_metadata.json')
    
def update_new_zpids_in_municipality(search_results, new_zpids_in_municipality, saved_zpids_in_municipality):
    current_zpid_set = {search_result['zpid'] for search_result in search_results}
    # Determine new zpids and so we can save only those.
    new_zpids = current_zpid_set - saved_zpids_in_municipality
    if force_visit_all_listings and not new_zpids:
        # We break if we have already explored all the zpids in this municipality (listings sorted by date).
        # We do not break if we have already seen these zpids from the MASTER_ZPID_SET since these could be due to no bound filters.
        return
    new_zpids -= MASTER_ZPID_SET
    new_zpids_in_municipality.update(new_zpids)
    MASTER_ZPID_SET.update(new_zpids)

def get_search_results_from_driver(driver):
    # Parse page source code.
    soup = BeautifulSoup(driver.page_source, 'html.parser')
    script_tag = soup.find('script', attrs={'id': '__NEXT_DATA__', 'type': 'application/json'})
    if not script_tag:
        return {}

    data = json.loads(script_tag.string)
    # If no house listings, stop searchign in this zip code.
    if not data or 'props' not in data or 'searchResults' not in data['props']['pageProps']['searchPageState']['cat1'] or 'listResults' not in data['props']['pageProps']['searchPageState']['cat1']['searchResults']:
        return {}
    
    return data['props']['pageProps']['searchPageState']['cat1']['searchResults']['listResults']

def scrape_listings():
    for municipality_ind, municipality in enumerate(all_municipalities):
        scrape_listings_in_municipality(municipality_ind, municipality)

def scrape_listings_in_municipality(municipality_ind, municipality):
    print(f'Searching for listings in municipality: {municipality} # [{municipality_ind+1} | {len(all_municipalities)}]', end='         \r')
    
    # Check whether this municipality was processed within the municipality cooldown window.
    if not should_process_municipality(municipality):
        return
    
    saved_zpids_in_municipality = municipality_to_zpids[municipality]
    new_zpids_in_municipality = set()
    search_results = []
    base_url = f"https://www.zillow.com/homes/{municipality}-fl/"

    # Make an initial request on the municipality Zillow page, we use this to grab region bounds for filtering homes outside the municipality.
    with get_selenium_driver("about:blank") as driver:
        driver.get(base_url)
        random_delay(0.25, 1)

        for page in range(1,scrape_config['max_pages_requested_per_zip']+1):
            new_search_results = get_search_results_from_driver(driver)
            search_results.extend([search_result for search_result in new_search_results if search_result['zpid'] not in MASTER_ZPID_SET])
            if not new_search_results:
                break
            update_new_zpids_in_municipality(new_search_results, new_zpids_in_municipality, saved_zpids_in_municipality)

            can_go_to_next_page = maybe_click_next_page(driver, page, skip_scrolling=True)
            if not can_go_to_next_page:
                break
        random_delay(0.25, 1)

    maybe_save_new_search_results(municipality, search_results, new_zpids_in_municipality, saved_zpids_in_municipality)
    random_delay(scrape_config['1s_delay'], scrape_config['2s_delay'])

should_filter_results, force_visit_all_listings = False, True
scrape_listings()
