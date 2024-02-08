import requests
from collections import defaultdict
from scraping_utility import *


import sys


def extract_price_and_tax_from_Zillow_GraphQL():
    search_results = load_json(scrape_config['search_results_path'])

    for search_result_ind, search_result in enumerate(search_results):
        zpid = search_result['zpid']
        zip_code = search_result['zip_code']
        property_url = search_result['url']

        property_path = f'PropertyDetails/{zip_code}/{zpid}_property_tax_and_price.json'
        if os.path.exists(property_path):
            continue
        ensure_directory_exists('/'.join(property_path.split('/')[:-1]))

        graphQL_url = "https://www.zillow.com/graphql/"
        querystring = {"zpid": zpid, "operationName": "PriceTaxQuery"}
        data_payload = "{\"query\":\"query PriceTaxQuery($zpid: ID!) {\\n  property(zpid: $zpid) {\\n    zpid\\n    livingArea\\n    countyFIPS\\n    parcelId\\n    taxHistory {\\n      time\\n      taxPaid\\n      taxIncreaseRate\\n      value\\n      valueIncreaseRate\\n    }\\n    priceHistory {\\n      time\\n      price\\n      priceChangeRate\\n      event\\n      source\\n      buyerAgent {\\n        photo {\\n          url\\n        }\\n        profileUrl\\n        name\\n      }\\n      sellerAgent {\\n        photo {\\n          url\\n        }\\n        profileUrl\\n        name\\n      }\\n      showCountyLink\\n      postingIsRental\\n    }\\n    currency\\n    country\\n  }\\n}\\n\",\"operationName\":\"PriceTaxQuery\",\"variables\":{\"zpid\":162029003},\"clientVersion\":\"home-details/5.48.1.0.0.hotfix-11-14-2019.6ee5cd9\"}"
        with get_selenium_driver(f"https://www.zillow.com/homes/{zip_code}_rb/") as driver:
                cookie_string = extract_cookies_from_driver(driver, scrape_config['cookie_sleep_delay'])

        headers = {
            'user-agent': USER_AGENT.random,
            'content-type': "application/json",
            'referer': property_url,
            'cookie': cookie_string,
        }
        response = requests.post(graphQL_url, data=data_payload, headers=headers, params=querystring, timeout=60)

        save_json(json.loads(response.text), property_path)

        num_search_results_digits = len(str(len(search_results)))
        formatted_search_result_ind = f"{search_result_ind+1:0{num_search_results_digits}d}"
        print(f'Scraped property: {zpid} in zip_code: {zip_code}, progress: [{formatted_search_result_ind}/{len(search_results)}]', end='\r')

        time.sleep(scrape_config['inter_property_delay'])

def delete_file(file_path):
    os.remove(file_path)
    print(f"Deleted {file_path}")
def delete_null_content_files(directory):
    for subdir, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(subdir, file)
            if file.endswith('.DS_Store'):
                delete_file(file_path)
            elif file.endswith('zestimate_history.json'):
                with open(file_path, 'r', encoding='utf-8') as f:
                    zestimate_history_data = json.loads(f.read().strip())
                    if not zestimate_history_data:
                        delete_file(file_path)
                        continue
                    zestimate_history = zestimate_history_data['data']['property']['homeValueChartData'][0]['points']
                    if len(zestimate_history) < 2:
                        delete_file(file_path)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content == 'null' or not content:
                        delete_file(file_path)
            except Exception as e:
                print(f"Error reading file {file_path}: {e}")

# Roughly 5 seconds per response -> ~ 14 hours for 10,000 properties/requests.
def extract_property_details_from_search_results():
    search_results = load_json(scrape_config['search_results_path'])

    for search_result_ind, search_result in enumerate(search_results):
        zpid = search_result['zpid']
        other_property_url = f"https://www.zillow.com/homedetails/{zpid}_zpid"
        zip_code = search_result['zip_code']

        num_search_results_digits = len(str(len(search_results)))
        formatted_search_result_ind = f"{search_result_ind+1:0{num_search_results_digits}d}"
        print(f'Scraping property: {zpid} in zip_code: {zip_code}, progress: [{formatted_search_result_ind}/{len(search_results)}]', end='         \r')

        property_path = f'PropertyDetails/{zip_code}/{zpid}_property_details.json'
        if os.path.exists(property_path):
            continue
        ensure_directory_exists('/'.join(property_path.split('/')[:-1]))
        
        # Parse the HTML content
        with get_selenium_driver(other_property_url) as driver:
            response = extract_property_details_from_driver(driver, scrape_config['inter_property_delay'])
             
        save_json(response, property_path)

        time.sleep(scrape_config['inter_property_delay'])


def extract_zestimate_history_from_graphQL():
    search_results_dir = "HomeData"

    # Initialize a list to hold the ZPID values
    zip_code_to_zpids = defaultdict(lambda: [])

    # Loop through all subdirectories in the HomeData directory
    for zip_code in os.listdir(search_results_dir):
        zip_code_path = os.path.join(search_results_dir, zip_code)
        if not os.path.isdir(zip_code_path):
            continue
        # Loop through all files in the ZIP code directory
        for file in os.listdir(zip_code_path):
            if not (file.startswith("listings_page_") and file.endswith(".json")):
                continue
            file_path = os.path.join(zip_code_path, file)
            # Open and read the JSON file
            with open(file_path, 'r') as json_file:
                data = json.load(json_file)
                zpids = [entry['zpid'] for entry in data]
                zip_code_to_zpids[zip_code].extend(zpids)

    url = "https://www.zillow.com/graphql"

    zip_code_ind = 0
    for zip_code, zpids in zip_code_to_zpids.items():
        # Retrieve a cookie for the sessions relating to this zip_code.
        with get_selenium_driver(f"https://www.zillow.com/homes/{zip_code}_rb/") as driver:
            cookie_string = extract_cookies_from_driver(driver, scrape_config['cookie_sleep_delay'])

        for zpid_ind, zpid in enumerate(zpids):
            print(f'Scraping property: {zpid} [{zpid_ind} / {len(zpids)}] in zip_code: {zip_code} [{zip_code_ind} / {len(zip_code_to_zpids)}]', end='         \r')

            property_path = f'PropertyDetails/{zip_code}/{zpid}_zestimate_history.json'
            if os.path.exists(property_path):
                continue

            payload = {
                "operationName": "HomeValueChartDataQuery",
                "variables": {
                    "zpid": zpid,
                    "timePeriod": "TEN_YEARS",
                    "metricType": "LOCAL_HOME_VALUES",
                    "forecast": True,
                    "useNewChartAPI": False
                },
                "query": "query HomeValueChartDataQuery($zpid: ID!, $metricType: HomeValueChartMetricType, $timePeriod: HomeValueChartTimePeriod, $useNewChartAPI: Boolean) {property(zpid: $zpid) {homeValueChartData(metricType: $metricType timePeriod: $timePeriod useNewChartAPI: $useNewChartAPI) {points {x y } name}}}"
            }
            headers = {
                "cookie": cookie_string,
                "content-type": "application/json",
                "user-agent": USER_AGENT.random
            }

            response = requests.request("POST", url, json=payload, headers=headers)
            save_json(json.loads(response.text), property_path)

            time.sleep(scrape_config['inter_property_delay'])

# extract_zestimate_history_from_graphQL()

# First we delete files which were not properly filled (likely due to connection and security).
base_directory = "PropertyDetails"
delete_null_content_files(base_directory)

# extract_property_details_from_search_results()
