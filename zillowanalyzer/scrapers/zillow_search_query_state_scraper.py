import os
import json
from bs4 import BeautifulSoup

from zillowanalyzer.utility.utility import (
    DATA_PATH, load_json, save_json, random_delay
)
from zillowanalyzer.scrapers.scraping_utility import get_selenium_driver, load_search_municipalities

MUNICIPALITIES_DATA_PATH = os.path.join(DATA_PATH, 'florida_municipalities_data.txt')
QUERY_STATE_DATA_PATH = os.path.join(DATA_PATH, 'query_state_data.json')


def get_municipality_query_state_data():
    if os.path.exists(QUERY_STATE_DATA_PATH):
        return load_json(QUERY_STATE_DATA_PATH)
    return {}

municipality_query_state_data = get_municipality_query_state_data()

def get_search_page_state_from_driver(driver):
    # Parse page source code.
    soup = BeautifulSoup(driver.page_source, 'html.parser')
    script_tag = soup.find('script', attrs={'id': '__NEXT_DATA__', 'type': 'application/json'})
    if not script_tag:
        return {}

    data = json.loads(script_tag.string)
    return data['props']['pageProps']['searchPageState']

def update_query_state_data(municipality, query_state_data):
    existing_data = get_municipality_query_state_data()
    existing_data[municipality] = query_state_data
    save_json(existing_data, QUERY_STATE_DATA_PATH)

def scrape_municipalities_data(all_municipalities):
    municipality_query_state_data = get_municipality_query_state_data()
    for municipality_ind, municipality in enumerate(all_municipalities):
        if municipality not in municipality_query_state_data:
            query_state_data = scrape_municipality_data(municipality)
            if query_state_data:
                update_query_state_data(municipality, query_state_data)
                print(f'Gathering query state data for municipality: {municipality} # [{municipality_ind+1} | {len(all_municipalities)}]')

def scrape_municipality_data(municipality):
    base_url = f"https://www.zillow.com/homes/{municipality}-fl/"
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
    county_to_municipalities = load_search_municipalities()
    all_municipalities = [municipality for county in county_to_municipalities.keys() for municipality in county_to_municipalities[county] if municipality not in municipality_query_state_data]
    scrape_municipalities_data(all_municipalities)
