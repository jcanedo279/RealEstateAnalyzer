import os
import json
from bs4 import BeautifulSoup

from zillowanalyzer.utility.utility import (
    DATA_PATH, load_json, save_json, random_delay, PROJECT_CONFIG
)
from zillowanalyzer.scrapers.scraping_utility import get_selenium_driver, load_search_zip_codes, retry_request


QUERY_STATE_DATA_PATH = os.path.join(DATA_PATH, 'query_state_data.json')

def get_zip_code_query_state_data():
    if os.path.exists(QUERY_STATE_DATA_PATH):
        return load_json(QUERY_STATE_DATA_PATH)
    return {}

zip_code_query_state_data = get_zip_code_query_state_data()

def get_search_page_state_from_driver(driver):
    # Parse page source code.
    soup = BeautifulSoup(driver.page_source, 'html.parser')
    script_tag = soup.find('script', attrs={'id': '__NEXT_DATA__', 'type': 'application/json'})
    if not script_tag:
        return {}

    data = json.loads(script_tag.string)
    return data['props']['pageProps']['searchPageState']

def update_query_state_data(zip_code, query_state_data):
    existing_data = get_zip_code_query_state_data()
    existing_data[zip_code] = query_state_data
    save_json(existing_data, QUERY_STATE_DATA_PATH)

def scrape_zip_codes_data(zip_codes):
    zip_code_query_state_data = get_zip_code_query_state_data()
    for zip_code_ind, zip_code in enumerate(zip_codes):
        if zip_code not in zip_code_query_state_data:
            query_state_data = scrape_zip_code_data(zip_code)
            if query_state_data:
                update_query_state_data(zip_code, query_state_data)
                print(f'Gathering query state data for zip code: {zip_code} # [{zip_code_ind+1} | {len(zip_codes)}]')

@retry_request(PROJECT_CONFIG)
def scrape_zip_code_data(zip_code):
    base_url = f"https://www.zillow.com/homes/{zip_code}_rb/"
    with get_selenium_driver("about:blank") as driver:
        driver.get(base_url)
        random_delay(2, 4)
        search_page_state_data = get_search_page_state_from_driver(driver)
        if not search_page_state_data:
            return None
        query_state_data = search_page_state_data['queryState']
        query_state_data['filterState']['sortSelection']['value'] = 'days'
        return query_state_data

if __name__ == '__main__':
    zip_codes = [zip_code for zip_code in load_search_zip_codes() if str(zip_code) not in zip_code_query_state_data]
    scrape_zip_codes_data(zip_codes)
