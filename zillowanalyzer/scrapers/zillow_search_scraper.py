import os
import requests
import json
import glob
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta

from zillowanalyzer.scrapers.scraping_utility import *


SEARCH_COOLDOWN_TIME_WINDOW = timedelta(days=1)

# Taken from: https://www.freemaptools.com/find-zip-codes-inside-user-defined-area.htm
# Keep this sorted in order to pick up where you left off.
TARGET_ZIP_CODES = [
    33002, 33004, 33008, 33009, 33010, 33011, 33012, 33013, 33014, 33015, 33016, 33017, 33018, 33019, 33020, 33021, 33022, 33023, 33024, 33025, 33026, 33027, 33028, 33029,
    33030, 33031, 33032, 33033, 33039, 33054, 33055, 33056, 33060, 33061, 33062, 33063, 33064, 33065, 33066, 33067, 33068, 33069, 33071, 33072, 33073, 33074, 33075,
    33076, 33077, 33081, 33082, 33083, 33084, 33090, 33092, 33093, 33097, 33101, 33102, 33106, 33109, 33111, 33112, 33114, 33116, 33119, 33122, 33124, 33125, 33126, 33127,
    33128, 33129, 33130, 33131, 33132, 33133, 33134, 33135, 33136, 33137, 33138, 33139, 33140, 33141, 33142, 33143, 33144, 33145, 33146, 33147, 33149, 33150, 33151,
    33152, 33153, 33154, 33155, 33156, 33157, 33158, 33160, 33161, 33162, 33163, 33164, 33165, 33166, 33167, 33168, 33169, 33170, 33172, 33173, 33174, 33175, 33176, 33177,
    33178, 33179, 33180, 33181, 33182, 33183, 33184, 33185, 33186, 33187, 33188, 33189, 33190, 33191, 33192, 33193, 33194, 33195, 33197, 33198, 33199, 33206, 33222,
    33231, 33233, 33234, 33238, 33239, 33242, 33243, 33245, 33247, 33255, 33256, 33257, 33261, 33265, 33266, 33269, 33280, 33283, 33296, 33299, 33301, 33302, 33303, 33304,
    33305, 33306, 33307, 33308, 33309, 33310, 33311, 33312, 33313, 33314, 33315, 33316, 33317, 33318, 33319, 33320, 33321, 33322, 33323, 33324, 33325, 33326, 33328,
    33329, 33330, 33331, 33332, 33334, 33335, 33336, 33337, 33338, 33339, 33340, 33345, 33346, 33348, 33349, 33351, 33355, 33359, 33388, 33394, 33427, 33428, 33429, 33431,
    33432, 33433, 33434, 33437, 33441, 33442, 33443, 33444, 33445, 33446, 33448, 33473, 33481, 33482, 33483, 33484, 33486, 33487, 33488, 33496, 33497, 33498, 33499
]
NUM_TARGET_ZIP_CODES = len(TARGET_ZIP_CODES)


def load_all_metadata(zip_code_to_zpids):
    """Load zpids from all metadata files into the zip_code_to_zpids mapping."""
    for file_path in glob.glob(os.path.join(SEARCH_RESULTS_METADATA_PATH, "*_metadata.json")):
        zip_code = os.path.basename(file_path).split("_metadata.json")[0]
        
        with open(file_path, "r") as file:
            zip_code_to_zpids[zip_code].update(json.load(file)['zpids'])

zip_code_to_zpids = defaultdict(set)
ensure_directory_exists(SEARCH_RESULTS_METADATA_PATH)
load_all_metadata(zip_code_to_zpids)
MASTER_ZPID_SET = {zpid for zpid_set in zip_code_to_zpids.values() for zpid in zpid_set}

def should_process_zip_code(zip_code):
    search_results_metadata_path = f'{SEARCH_RESULTS_METADATA_PATH}/{zip_code}_metadata.json'
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

def scrape_listings():
    for zip_code_index, zip_code in enumerate(TARGET_ZIP_CODES):
        scrape_listings_in_zip_code(zip_code_index, zip_code)
        random_delay(scrape_config['5s_delay'], scrape_config['10s_delay'])

def scrape_listings_in_zip_code(zip_code_index, zip_code):
    print(f'Searching for listings in zip_code: {zip_code} #: [{zip_code_index+1} / {NUM_TARGET_ZIP_CODES}]', end='         \r')
    zip_code_path = f"{SEARCH_RESULTS_DATA_PATH}/{zip_code}"

    # Check whether this zip_code was processed within the zip_code cooldown window.
    if not should_process_zip_code(zip_code):
        return
    
    existing_zpid_set = zip_code_to_zpids[zip_code]
    zpids_to_save = set()
    base_url = f"https://www.zillow.com/homes/{zip_code}_rb/1_p/"

    # Make an initial request on the zip_code Zillow page, we use this to grab region bounds for filtering homes outside the zip_code.
    with get_selenium_driver(base_url) as driver:
        # If a Captcha is detected, stop scraping.
        try:
            while driver.find_element(By.ID, 'px-captcha-wrapper'):
                print("I need halp :<")
                time.sleep(20)
        except:
            pass

        region_data = extract_region_data_from_driver(driver, scrape_config['2s_delay'])
        random_delay(scrape_config['1s_delay'], scrape_config['2s_delay'])

        search_query_state = {
            "isMapVisible": False,
            "mapBounds": region_data['mapBounds'],
            "regionSelection": region_data['regionSelection'],
            "filterState": {
                "sort": {"value": SORT_LISTING_BY_ENUM_TO_STRING[scrape_config['sort_listing_by']]},
                "ah": {"value": True},
                "beds": {"min": scrape_config['min_num_bedrooms']},
                "baths": {"min": scrape_config['min_num_bathrooms']},
                "land": {"value": False},
                "hoa": {"max": scrape_config['max_hoa']},
                "nohoa": {"value": False},
                "sqft": {"min": scrape_config['min_home_sqft']},
                "lot": {"min": scrape_config['min_lot_sqft']},
                "built": {"min": scrape_config['min_year_built']},
                "55plus": {"value": "e"},
                "manu": {"value": scrape_config['show_manufactured_listings']},
                "land": {"value": scrape_config['show_lots_land']},
            },
            "isListVisible": True
        }
        search_query_state_json = json.dumps(search_query_state)
        encoded_search_query_state = urllib.parse.quote(search_query_state_json)

        # Append the encoded search_query_state to the base URL.
        full_url = base_url + "?searchQueryState=" + encoded_search_query_state
        # Make the initial request to the target listings.
        driver.get(full_url)
        try:
            while driver.find_element(By.ID, 'px-captcha-wrapper'):
                print("I need halp :<")
                time.sleep(20)
        except:
            pass
        random_delay(scrape_config['2s_delay'], scrape_config['5s_delay'])

        for page in range(1,scrape_config['max_pages_requested_per_zip']+1):

            # Parse page source code.
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            script_tag = soup.find('script', attrs={'id': '__NEXT_DATA__', 'type': 'application/json'})
            if not script_tag:
                break

            data = json.loads(script_tag.string)
            # If no house listings, stop searchign in this zip code.
            if not data or 'props' not in data or 'searchResults' not in data['props']['pageProps']['searchPageState']['cat1']:
                break
            
            # Process and save search results for this page.
            search_results = data['props']['pageProps']['searchPageState']['cat1']['searchResults']['listResults']
            current_zpid_set = {search_result['zpid'] for search_result in search_results}

            # Determine new zpids and save only those.
            new_zpids = current_zpid_set - existing_zpid_set - MASTER_ZPID_SET
            if not new_zpids:
                break
            zpids_to_save.update(new_zpids)
            MASTER_ZPID_SET.update(new_zpids)

            try:
                next_page_link = driver.find_element(By.CSS_SELECTOR, f'a[title="Page {page+1}"]')
                scroll_to_element(driver, "#search-page-list-container", ".search-pagination")
                if next_page_link:
                    move_to_and_click(next_page_link, driver)
                    random_delay(scrape_config['2s_delay'], scrape_config['5s_delay'])
                else:
                    break
            except NoSuchElementException:
                break
            except TimeoutException:
                break

    new_search_results = [search_result for search_result in search_results if search_result['zpid'] in zpids_to_save]
    if new_search_results:
        current_datetime = datetime.now()
        ensure_directory_exists(zip_code_path)
        save_json(new_search_results, f'{zip_code_path}/listings_{current_datetime.strftime("%Y-%m-%d_%H-%M")}.json')
        existing_zpid_set.update(zpids_to_save)

        search_metadata = {
            'zpids': list(existing_zpid_set),
            'last_checked': current_datetime.isoformat()
        }
        save_json(search_metadata, f'{SEARCH_RESULTS_METADATA_PATH}/{zip_code}_metadata.json')


scrape_listings()
