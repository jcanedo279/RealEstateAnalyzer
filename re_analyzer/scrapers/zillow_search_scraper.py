import os
import json
import time
import random as rd
from datetime import datetime, timedelta

from re_analyzer.utility.utility import (
    DATA_PATH, SEARCH_LISTINGS_METADATA_PATH, SEARCH_LISTINGS_DATA_PATH,
    ensure_directory_exists, load_json, random_delay, is_within_cooldown_period, save_json, batch_generator, backoff_strategy
)
from re_analyzer.scrapers.scraping_utility import get_selenium_driver, kill_chrome_leaks, load_search_metadata


###########
## SETUP ##
###########

def should_process_zip_code(zip_code):
    """
        Determines if a zip code should be processed based on cooldown period.
    """
    metadata_path = os.path.join(SEARCH_LISTINGS_METADATA_PATH, f"{zip_code}_metadata.json")
    if not os.path.exists(metadata_path):
        return True  # Process if no metadata exists
    metadata = load_json(metadata_path)
    return not is_within_cooldown_period(metadata.get('last_checked'), COOLDOWN_PERIOD)


# Global configuration.
COOLDOWN_PERIOD = timedelta(hours=36)
QUERY_STATE_DATA_PATH = os.path.join(DATA_PATH, 'query_state_data.json')
zip_code_to_seen_zpid_tuple = set()

# Ensure necessary directories exist.
ensure_directory_exists(SEARCH_LISTINGS_METADATA_PATH)

# Load initial data.
zip_code_to_zpids = load_search_metadata()
all_known_zpids = {zpid for zpids in zip_code_to_zpids.values() for zpid in zpids}
query_state_data = load_json(QUERY_STATE_DATA_PATH) if os.path.exists(QUERY_STATE_DATA_PATH) else {}
zip_code_to_query_state_data = {
    zip_code: data for zip_code, data in query_state_data.items()
    if should_process_zip_code(zip_code)
}


#####################
## SEARCH SCRAPERS ##
#####################

def scrape_listings(batch_size=10):
    """
        Main function to orchestrate the scraping process in batches.
    """
    zip_code_to_query_state_data_items = rd.sample(
        list(zip_code_to_query_state_data.items()), 
        len(zip_code_to_query_state_data)
    )
    
    if not zip_code_to_query_state_data_items:
        print("No query state data found or all zip codes are within cooldown period.")
        return
    
    zip_code_ind = 0
    for batch in batch_generator(zip_code_to_query_state_data_items, batch_size):
        with get_selenium_driver("about:blank", ignore_detection=True) as driver:
            user_agent, cookie_string = initialize_driver_session_for_zip_code(driver, batch[0][0])
            for zip_code, query_state_data in batch:
                scrape_zip_code(driver, user_agent, cookie_string, zip_code, zip_code_ind, query_state_data)
                zip_code_ind += 1
        kill_chrome_leaks()


def scrape_zip_code(driver, user_agent, cookie_string, zip_code, zip_code_ind, query_state_data):
    """
        Scrapes listings for a single zip code.
    """
    search_results = []
    page = 1
    total_pages = 1
    attempt = 1

    while page <= total_pages:
        print(f'Searching for listings in zip code: {zip_code} # [{zip_code_ind+1} | {len(zip_code_to_query_state_data.keys())}]  page [{page} | {total_pages}]', end='         \r')
        search_data = scrape_listings_in_zip_code_for_page(driver, user_agent, cookie_string, query_state_data, page)
        if not search_data:
            return
        while not process_search_results(zip_code, search_results, search_data):
            backoff_strategy(attempt)
            attempt += 1
            search_data = scrape_listings_in_zip_code_for_page(driver, user_agent, cookie_string, query_state_data, page)
        if total_pages == 1:
            total_pages = min(search_data['cat1']['searchList']['totalPages'], 20)
        page += 1
    print(f"\nFound {len(search_results)} in {zip_code}")
    if not search_results:
        return
    maybe_save_current_search_results(zip_code, search_results)

def scrape_listings_in_zip_code_for_page(driver, user_agent, cookie_string, query_state_data, page):
    """
        Fetches listings for a specific page within a zip code's search results.
    """
    query_state_data["pagination"] = {"currentPage": page}
    js_code = f"""
    (async () => {{
        const response = await fetch("https://www.zillow.com/async-create-search-page-state", {{
            method: "PUT",
            headers: {{
                "content-type": "application/json",
                "cookie": "{cookie_string}",
                "user-agent": "{user_agent}"
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
        window.fetchData = data;
    }})();
    """

    driver.execute_script(js_code)
    random_delay(1, 2)

    # Wait for data to asynchronously update for a maximum of 10 seconds.
    data, start_time = None, time.time()
    while not data and time.time() < start_time + 10:
        data = driver.execute_script("return window.fetchData;")
        time.sleep(0.1)
    return data


#######################
## SEARCH PROCESSING ##
#######################

def process_search_results(zip_code, search_results, search_data):
    """
        Processes search results, filtering and updating global sets.
    """
    
    current_search_results = [search_result for search_result in search_data['cat1']['searchResults']['listResults']]
    current_search_zpids = tuple(search_result['zpid'] for search_result in current_search_results)
    # We should back off if we have seen this ordered tuple of zpids (likely from faked server data).
    if current_search_zpids in zip_code_to_seen_zpid_tuple:
        return False
    # Filter out visited zpids and results from other zip codes.
    current_search_results = [search_result for search_result in current_search_results if search_result['zpid'] not in all_known_zpids and int(search_result.get('addressZipcode', -1)) == int(zip_code)]
    current_search_zpids = tuple(search_result['zpid'] for search_result in current_search_results)
    # Its posible we have real data but have either processed or should not process the results, we should not back off.
    if not current_search_results:
        return True
    zip_code_to_seen_zpid_tuple.add(current_search_zpids)
    search_results.extend(current_search_results)
    all_known_zpids.update(current_search_zpids)
    return True

def maybe_save_current_search_results(zip_code, search_results):
    """
        Saves search results and updates metadata for a zip code.
    """
    search_results_zpids = [new_search_result['zpid'] for new_search_result in search_results]
    zip_code_to_zpids[zip_code].update(search_results_zpids)

    search_metadata = {
        'zpids': list(zip_code_to_zpids[zip_code]),
        'active_zpids': list(search_results_zpids),
        'last_checked': datetime.now().isoformat()
    }
    save_json(search_metadata, os.path.join(SEARCH_LISTINGS_METADATA_PATH, f"{zip_code}_metadata.json"))

    zip_code_path = os.path.join(SEARCH_LISTINGS_DATA_PATH, zip_code)
    # We save even if new_search_results is empty to ensure the metadata exists to skip over this zip code.
    ensure_directory_exists(zip_code_path)
    formatted_current_time = datetime.now().strftime("%Y-%m-%d_%H-%M")
    save_json(search_results, os.path.join(zip_code_path, f"listings_{formatted_current_time}.json"))


#################
## SEARCH UTIL ##
#################

def initialize_driver_session_for_zip_code(driver, zip_code):
    """
        Initializes a web session for the given zip code.
    """
    base_url = f"https://www.zillow.com/homes/{zip_code}_rb/"
    driver.get(base_url)
    user_agent = driver.execute_script("return navigator.userAgent;")
    cookie_string = '; '.join([f'{cookie["name"]}={cookie["value"]}' for cookie in driver.get_cookies()])
    return user_agent, cookie_string



if __name__ == "__main__":
    scrape_listings()
