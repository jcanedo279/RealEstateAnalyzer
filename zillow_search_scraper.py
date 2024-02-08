import time
import os
import requests
import json
from scraping_utility import *


search_metadata_path = scrape_config['search_metadata_path']
sort_listing_by = scrape_config['sort_listing_by']
previous_search_metadata = load_json(search_metadata_path)
STARTING_ZIP_CODE = previous_search_metadata.get("search_progress", {}).get("zip_code", 0)
STARTING_PAGE = previous_search_metadata.get("search_progress", {}).get("page", 0)


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


zpid_list = load_json('zpid_list.json')
zpid_hash = set(zpid_list)

def fetch_data_from_backend(payload, cookie_string):
    headers = {
        "cookie": cookie_string,
        "content-type": "application/json",
        "user-agent": USER_AGENT.random
    }
    try:
        response = requests.put(scrape_config['zillow_search_request'], json=payload, headers=headers)
        return json.loads(response.text)
    except Exception as e:
        with open('error.txt', 'w', encoding='utf-8') as file:
            file.write(response.text)
        return

def get_house_metadata(data):
    return {
        "user_info": data.get("user", {}),
        "map_state": data.get("mapState", {}),
        "region_state": data.get("regionState", {}),
        "search_page_seo": data.get("searchPageSeoObject", {}),
        "request_id": data.get("requestId", None)
    }

def scrape_listings():
    for zip_code_index, zip_code in enumerate(TARGET_ZIP_CODES):
        if zip_code < STARTING_ZIP_CODE:
            continue
        scrape_listings_in_zip_code(zip_code_index, zip_code)
        time.sleep(scrape_config['inter_zip_code_delay'])


def scrape_listings_in_zip_code(zip_code_index, zip_code):
    scrape_results_path = f"HomeData/{zip_code}"
    metadata_path = f'{scrape_results_path}/metadata.json'

    directory_path = f'HomeData/{zip_code}'

    with get_selenium_driver(f"https://www.zillow.com/homes/{zip_code}_rb/") as driver:
        cookie_string = extract_cookies_from_driver(driver, scrape_config['cookie_sleep_delay'])
        region_data = extract_region_data_from_driver(driver, scrape_config['region_sleep_delay'])
    if not cookie_string or not region_data:
        return

    max_pages_requested_per_zip = scrape_config['max_pages_requested_per_zip']
    # Loop over the pages of listings.
    for page in range(1,max_pages_requested_per_zip+1):
        if zip_code == STARTING_ZIP_CODE and page <= STARTING_PAGE:
            continue

        payload = {
            "searchQueryState": {
                "pagination": {"currentPage": page},
                "isMapVisible": False,
                "mapBounds": region_data['mapBounds'],
                "regionSelection": region_data['regionSelection'],
                "filterState": {
                    "sortSelection": {
                        "value": SORT_LISTING_BY_ENUM_TO_STRING[sort_listing_by]
                    },
                    "built": {
                        "min": scrape_config['min_year_built']
                    },
                    "isAllHomes": {
                        "value": True
                    },
                    "hoa": {
                        "max": scrape_config['max_hoa']
                    }
                },
                "isListVisible": True,
                "mapZoom": 13
            },
            "wants": {
                "cat1": ["listResults"],
                "cat2": ["total"]
            },
            "requestId": 4,
            "isDebugRequest": False
        }
        if scrape_config['min_num_bedrooms']:
            payload['searchQueryState']['filterState']['beds'] = {
                "min": scrape_config['min_num_bedrooms']
            }
        if scrape_config['min_num_bathrooms']:
            payload['searchQueryState']['filterState']['baths'] = {
                "min": scrape_config['min_num_bathrooms']
            }
        if scrape_config['min_home_sqft']:
            payload['searchQueryState']['filterState']['sqft'] = {
                "min": scrape_config['min_home_sqft']
            }
        if scrape_config['min_lot_sqft']:
            payload['searchQueryState']['filterState']['lotSize'] = {
                "min": scrape_config['min_lot_sqft']
            }
        if scrape_config['show_homes_in_55_plus_communities']:
            payload['searchQueryState']['filterState']['ageRestricted55Plus'] = {
                "value": "e"
            }
        if scrape_config['show_auction_listings']:
            payload['searchQueryState']['filterState']['isAuction'] = {
                "value": scrape_config['show_auction_listings']
            }
        if scrape_config['show_new_construction_listings']:
            payload['searchQueryState']['filterState']['isNewConstruction'] = {
                "value": scrape_config['show_new_construction_listings']
            }
        if scrape_config['show_manufactured_listings']:
            payload['searchQueryState']['filterState']['isManufactured'] = {
                "value": scrape_config['show_manufactured_listings']
            }
        if scrape_config['show_lot_land_listing']:
            payload['searchQueryState']['filterState']['isLotLand'] = {
                "value": scrape_config['show_lot_land_listing']
            }
        if scrape_config['show_homes_with_no_hoa_data']:
            payload['searchQueryState']['filterState']['includeHomesWithNoHoaData'] = {
                "value": scrape_config['show_homes_with_no_hoa_data']
            }
    

        # Make the call to the backend for the specigic page, page data and the zip code is fed through the payload.
        data = fetch_data_from_backend(payload, cookie_string)
        
        # If no house listings, stop searchign in this zip code.
        if not data or 'cat1' not in data or 'searchResults' not in data['cat1']:
            break
        ensure_directory_exists(directory_path)

        # Optionally save metadata.
        if not os.path.exists(metadata_path):
            metadata = get_house_metadata(data)
            save_json(metadata, metadata_path)
        
        listings = data['cat1']['searchResults']['listResults']
        save_json(listings, f'{directory_path}/listings_page_{page}.json')

        # Update for each page within the same zipcode
        total_pages = data.get('cat1', {}).get('searchList', {}).get('totalPages', max_pages_requested_per_zip)
        max_pages = min(total_pages, max_pages_requested_per_zip)
        max_page_digits = len(str(max_pages))
        formatted_page_number = f"{page:0{max_page_digits}d}"

        num_zipcode_digits = len(str(len(TARGET_ZIP_CODES)))
        formatted_zip_code_index = f"{zip_code_index+1:0{num_zipcode_digits}d}"
        print(f'Scraped page [{formatted_page_number}/{max_pages}] of zipcode {zip_code} number [{formatted_zip_code_index}/{len(TARGET_ZIP_CODES)}]', end='\r')

        # Save metadata regarding our search progress so we can pickup where we left off.
        search_metadata = {
            "search_progress": {
                "zip_code": zip_code,
                "page": page
            },
            "filter_state": payload["searchQueryState"]["filterState"]
        }
        save_json(search_metadata, search_metadata_path)

        # If there is no next page we exit, otherwise we will re-generate results.
        if page >= data['cat1']['searchList']['totalPages']:
            break

        time.sleep(scrape_config['inter_page_delay'])

scrape_listings()
