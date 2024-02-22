import sys
import requests
import urllib
import json

from zillowanalyzer.scrapers.scraping_utility import DATA_PATH, save_json, get_selenium_driver


def format_payload(city, state="FL"):
    encoded_city = urllib.parse.quote_plus(city)
    payload = f"city={encoded_city}&state={state}"
    return payload

url = "https://tools.usps.com/tools/app/ziplookup/zipByCityState"

municipalities = []
with open(f'{DATA_PATH}/florida_municipalities_overflowing.txt', 'r') as munici_file:
    municipalities = [municipality.strip() for municipality in munici_file.readlines()]

with get_selenium_driver("https://tools.usps.com/zip-code-lookup.htm") as driver:
    cookie_string = '; '.join([f'{cookie["name"]}={cookie["value"]}' for cookie in driver.get_cookies()])
    user_agent = driver.execute_script("return navigator.userAgent;")

municipality_to_zip_codes = {}
for municipality_ind, municipality in enumerate(municipalities):
    print(f"{municipality} number [{municipality_ind} | {len(municipalities)}]", end='         \r')
    payload = format_payload(municipality)
    headers = {
        "cookie": cookie_string,
        "user-agent": user_agent,
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8"
    }
    try:
        response = requests.request("POST", url, data=payload, headers=headers)
    except:
        continue
    response_data = json.loads(response.text)
    if not response or not response_data:
        continue

    # Extract zip codes
    zip_codes = [ item['zip5'] for item in response_data['zipList'] ]
    if not zip_codes:
        continue

    municipality_to_zip_codes[municipality] = zip_codes

save_json(municipality_to_zip_codes, f'{DATA_PATH}/florida_overflowing_municipalities_to_zip_codes.json')
