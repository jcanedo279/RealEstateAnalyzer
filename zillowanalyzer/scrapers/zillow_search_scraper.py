import os
import json
import glob
import re
from collections import defaultdict
from datetime import datetime, timedelta

from zillowanalyzer.scrapers.scraping_utility import *


SEARCH_COOLDOWN_TIME_WINDOW = timedelta(days=2)
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

def maybe_save_current_search_results(municipality, search_results):
    municipality_path = f"{SEARCH_RESULTS_DATA_PATH}/{municipality}"
    # We save even if new_search_results is empty to ensure the metadata exists to skip over this municipality.
    current_datetime = datetime.now()
    ensure_directory_exists(municipality_path)
    save_json(search_results, f'{municipality_path}/listings_{current_datetime.strftime("%Y-%m-%d_%H-%M")}.json')
    municipality_to_zpids[municipality].update([new_search_result['zpid'] for new_search_result in search_results])

    search_metadata = {
        'zpids': list(municipality_to_zpids[municipality]),
        'last_checked': current_datetime.isoformat()
    }
    save_json(search_metadata, f'{SEARCH_RESULTS_METADATA_PATH}/{municipality}_metadata.json')

def get_search_page_state_from_driver(driver):
    # Parse page source code.
    soup = BeautifulSoup(driver.page_source, 'html.parser')
    script_tag = soup.find('script', attrs={'id': '__NEXT_DATA__', 'type': 'application/json'})
    if not script_tag:
        return {}

    data = json.loads(script_tag.string)
    save_json(data, 'data.json')
    return data['props']['pageProps']['searchPageState']

def scrape_listings():
    for municipality_ind, municipality in enumerate(all_municipalities):
        scrape_listings_in_municipality(municipality_ind, municipality)

def scrape_listings_in_municipality(municipality_ind, municipality):
    # Check whether this municipality was processed within the municipality cooldown window.
    if not should_process_municipality(municipality):
        return
    
    search_results = []
    base_url = f"https://www.zillow.com/homes/{municipality}-fl/"

    # Make an initial request on the municipality Zillow page, we use this to grab region bounds for filtering homes outside the municipality.
    with get_selenium_driver("about:blank") as driver:
        driver.get(base_url)
        random_delay(1, 2)
        search_page_state_data = get_search_page_state_from_driver(driver)
        if not search_page_state_data:
            # Sometimes a municipality either DNE or has no homes, in which case we exit.
            maybe_save_current_search_results(municipality, search_results)
            return
        total_pages = search_page_state_data['cat1']['searchList']['totalPages']
        # total_results = search_page_state_data['cat1']['searchList']['totalResultCount']
        query_state_data = search_page_state_data['queryState']
        save_json(search_page_state_data, 'search_page_state_data.json')

        for page in range(1,total_pages+1):
            # print(filter_state)
            print(f'Searching for listings in municipality: {municipality} # [{municipality_ind+1} | {len(all_municipalities)}]  page [{page} | {total_pages}]', end='         \r')
            query_state_data["pagination"] = {"currentPage": page}
            js_code = f"""
                (async () => {{
                    const response = await fetch("https://www.zillow.com/async-create-search-page-state", {{
                        method: "PUT",
                        headers: {{
                            "content-type": "application/json",
                            "cookie": "{'; '.join([f'{cookie["name"]}={cookie["value"]}' for cookie in driver.get_cookies()])}",
                            "user-agent": "{driver.execute_script("return navigator.userAgent;")}"
                        }},
                        body: JSON.stringify({{
                            "searchQueryState": {json.dumps(query_state_data)},
                            "wants": {{
                                "cat1": ["listResults", "mapResults"],
                                "cat2": ["total"]
                            }},
                            "requestId": 5,
                            "isDebugRequest": false
                        }})
                    }});
                    const data = await response.json();
                    window.fetchData = data; // Store the data in a global variable for Selenium to access
                }})();
                """

            driver.execute_script(js_code)
            data, start_time = None, time.time()
            # Wait for data to asynchronously update for a maximum of 10 seconds.
            while not data and time.time() < start_time + 10:
                data = driver.execute_script("return window.fetchData;")
                time.sleep(0.1)
            
            if not data:
                sys.exit()
            current_search_results = [search_result for search_result in data['cat1']['searchResults']['listResults'] if search_result['zpid'] not in MASTER_ZPID_SET]
            search_results.extend(current_search_results)
            MASTER_ZPID_SET.update([search_result['zpid'] for search_result in current_search_results])
            random_delay(1, 2)

    maybe_save_current_search_results(municipality, search_results)
    random_delay(scrape_config['1s_delay'], scrape_config['2s_delay'])

if __name__ == '__main__':
    should_filter_results, force_visit_all_listings = False, True
    # scrape_listings()
