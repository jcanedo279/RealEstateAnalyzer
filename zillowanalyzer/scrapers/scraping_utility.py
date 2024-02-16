import os
import time
import json
import re
import sys
import configparser
import random as rd
from enum import Enum, auto
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from fake_useragent import UserAgent
from functools import wraps
from contextlib import contextmanager
from dotenv import load_dotenv
from seleniumwire import webdriver
import requests
from selenium.common.exceptions import *
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from dateutil.parser import parse
import pandas as pd
from selenium.webdriver.common.action_chains import ActionChains


# Chromium versions found at: https://vikyd.github.io/download-chromium-history-version/#/

CONFIG_PATH = 'zillowanalyzer/scrapers/scrape_config.cfg'
CHROME_BINARY_EXECUTABLE_PATH = "zillowanalyzer/ChromeAssets/GC_121_0_6167_85.app/Contents/MacOS/Google Chrome for Testing"
# Use chrome://version/ to locate the user_data_dir path.
CHROME_USER_DATA_DIR = "/Users/jorgecanedo/Library/Application Support/Google/Chrome for Testing"
local_path_exists = os.path.exists(CHROME_USER_DATA_DIR)


class SortListingBy(Enum):
    PRICE_DESC = auto()
    PRICE_ASC = auto()
    NEWEST = auto()
SORT_LISTING_BY_ENUM_TO_STRING = {
    SortListingBy.PRICE_DESC: "priced",
    SortListingBy.PRICE_ASC: "pricea",
    SortListingBy.NEWEST: "days"
}

class ProxyType(Enum):
    HTTP = auto()
    HTTPS = auto()
    SOCKS4 = auto()
    SOCKS5 = auto()


def save_json(data, path):
    with open(path, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=4)
def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as file:
        return json.load(file)

def ensure_directory_exists(path):
    if not os.path.exists(path):
        os.makedirs(path)

def convert_to_enum(enum_type, value):
    for enum_member in enum_type:
        if enum_member.name == value:
            return enum_member
    raise ValueError(f"Invalid enum value: {value}")

class ScrapeConfigManager:
    def __init__(self, ):
        self.config_dict = {}
        self.enum_types = [SortListingBy]
        self.load_config()

    def load_config(self):
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH)

        for section in config.sections():
            for key, value in config.items(section):
                if key == 'sort_listing_by':
                    self.config_dict[key] = convert_to_enum(SortListingBy, value)
                else:
                    self.config_dict[key] = self.parse_config_value(value)

    def parse_config_value(self, value):
        # Assuming 'sort_listing_by' is the only key that needs enum conversion
        if value in [enum_member.name for enum_member in SortListingBy]:
            return self.parse_enum(value)
        else:
            parsers = [
                self.parse_bool,
                self.parse_int,
                self.parse_float,
                self.parse_string
            ]
            for parser in parsers:
                result = parser(value)
                if result is not None:
                    return result

    def parse_bool(self, value):
        if value.lower() in ['true', 'false']:
            return value.lower() == 'true'
        return None

    def parse_int(self, value):
        try:
            return int(value)
        except ValueError:
            return None

    def parse_float(self, value):
        try:
            return float(value)
        except ValueError:
            return None

    def parse_enum(self, value):
        convert_to_enum(SortListingBy, value)

    def parse_string(self, value):
        return value

    def __getitem__(self, key, default=None):
        return self.config_dict.get(key, default)

class CookieManager:
    def __init__(self):
        self.cookie_string = None
        self.zip_code = None

    def get_cookie_for_zip_code(self, zip_code):
        if zip_code != self.zip_code or self.cookie_string is None:
            self.zip_code = zip_code
            self.cookie_string = self.fetch_cookie_from_zip_code(zip_code)
        return self.cookie_string

    def fetch_cookie_from_zip_code(self, zip_code):
        # Simulate fetching a cookie string for the given zip code
        with get_selenium_driver(f"https://www.zillow.com/homes/{zip_code}_rb/") as driver:
            return extract_cookies_from_driver(driver, scrape_config['3s_delay'])

def get_fake_headers_list():
  response = requests.get(f'http://headers.scrapeops.io/v1/browser-headers?api_key={SCRAPEOPS_API_KEY}')
  json_response = response.json()
  return json_response.get('result', [])

# Fetch the ScrapeConfig to use it for the rest of the utility file.
scrape_config = ScrapeConfigManager()
load_dotenv()
SCRAPEOPS_API_KEY = os.environ.get('SCRAPE_OPS_API_KEY')
if not SCRAPEOPS_API_KEY:
    sys.exit("A SCRAPEOPS_API_KEY is required to generate plausible headers when scraping :<")
GENERATED_HEADERS = get_fake_headers_list()
USER_AGENT = UserAgent()
cookie_manager = CookieManager()
# proxy_manager = ProxyManager()

DATA_PATH = scrape_config['data_path']
VISUAL_DATA_PATH = scrape_config['visual_data_path']
HOME_DATA_PATH = scrape_config['home_data_path']
PROPERTY_DETAILS_PATH = scrape_config['property_details_path']
SEARCH_RESULTS_PATH = scrape_config['search_results_path']

def get_chrome_options(headless=False, incognito=False):
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless")
        # The following options help mitigate the detectability of headless browsers.
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-setuid-sandbox")
    if incognito:
        options.add_argument("--incognito")
    elif local_path_exists:
        user_data_dir = CHROME_USER_DATA_DIR
        profile_directory = f"Profile {rd.randint(5, 9)}"
        options.add_argument(f"--user-data-dir={user_data_dir}")
        options.add_argument(f"--profile-directory={profile_directory}")
    options.add_argument('--ignore-ssl-errors=yes')
    options.add_argument('--ignore-certificate-errors')
    options.add_argument(f"--window-size={int(rd.uniform(700, 1400))},{rd.uniform(400,900)}")
    options.add_argument("--disable-dev-shm-usage")
    return options

@contextmanager
def get_selenium_driver(url, headless=False, incognito=False):
    # proxy_wrapper = proxy_manager.get_proxy_wrapper()
    binary_executable_path = CHROME_BINARY_EXECUTABLE_PATH
    options = get_chrome_options(headless=headless, incognito=incognito)
    driver = uc.Chrome(options=options, browser_executable_path=binary_executable_path)
    driver.get(url)
    try:
        yield driver
    finally:
        driver.quit()

@contextmanager
def get_selenium_wire_driver(url, headless=False, incognito=False):
    # proxy_wrapper = proxy_manager.get_proxy_wrapper()
    binary_executable_path = CHROME_BINARY_EXECUTABLE_PATH
    options = get_chrome_options(headless=headless, incognito=incognito)
    driver = webdriver.Chrome(options=options, binary_executable_path=binary_executable_path)
    driver.get(url)
    try:
        yield driver
    finally:
        driver.quit()

def retry_request(scrape_config):
    max_attempts = scrape_config['max_reconnect_retries']
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempts in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempts >= max_attempts:
                        break
                    random_delay(scrape_config['5s_delay'], scrape_config['10s_delay'])
            random_delay(scrape_config['15s_delay'], scrape_config['30s_delay'])
            return None
        return wrapper
    return decorator


@retry_request(scrape_config)
def extract_cookies_from_driver(driver, delay):
    random_delay(delay, 2*delay)
    cookies = driver.get_cookies()
    session_cookies = {cookie['name']: cookie['value'] for cookie in cookies}
    return "; ".join([f"{name}={value}" for name, value in session_cookies.items()])

@retry_request(scrape_config)
def extract_region_data_from_driver(driver, delay):
    random_delay(delay, 2*delay)
    soup = BeautifulSoup(driver.page_source, 'html.parser')
    script_tag = soup.find('script', string=re.compile('regionSelection'))
    if script_tag:
        script_content = script_tag.string
        return json.loads(script_content)['props']['pageProps']['searchPageState']['queryState']
    return None

@retry_request(scrape_config)
def extract_property_details_from_driver(driver, delay):
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

    # Check and parse 'topnav'
    topnav = json_data['props']['pageProps']['pageFrameProps']['pageFrameData'].get('topnav')
    if isinstance(topnav, str):
        json_data['props']['pageProps']['pageFrameProps']['pageFrameData']['topnav'] = json.loads(topnav)

    return json_data

def parse_dates(date_str):
    # Define your expected date formats
    formats = ["%b %Y", "%m/%d/%y", "%Y-%m-%d"]
    
    # Try parsing according to each possible date format.
    for fmt in formats:
        try:
            return pd.to_datetime(date_str, format=fmt)
        except ValueError:
            continue
    
    # Fallback parser if none of the formats match.
    try:
        return parse(date_str)
    except ValueError:
        # Return Not-a-Time for unparseable formats.
        return pd.NaT

def extract_zestimate_history_from_driver(driver, delay):
    try:
        container = driver.find_element(By.ID, "ds-home-values")
    except NoSuchElementException:
        # There is no Zestimate history for this home (likely its off market).
        return []

    scroll_to_element(driver, "ds-home-values")
    # time.sleep(10000)

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
            move_to_and_click(show_more_button, driver)
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
    move_to_and_click(table_view_button, driver)
    
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

def what_is_my_ip():
    what_is_my_ip_url = "http://httpbin.org/ip"
    with get_selenium_driver(what_is_my_ip_url) as driver:
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        try:
            pre_tag_content = soup.find('pre').text
            json_data = json.loads(pre_tag_content)
            ip_address_text = json_data["origin"]
            ip_addresses = [ip.strip() for ip in ip_address_text.split(',')]
            return ip_addresses
        except:
            return
        
# reference_url is used as the previous url which we are coming from.
# zip_code is used more generally by the cookie_manager to allow all requests in a zip_code to re-use the same cookie_string.
def gen_headers_for_zillow(zip_code, reference_url, client_id=None):
    generated_headers = rd.choice(GENERATED_HEADERS)
    headers = {
        'cookie': cookie_manager.get_cookie_for_zip_code(zip_code),
        'authority': "www.zillow.com",
        'accept': "*/*",
        'accept-language': generated_headers['accept-language'],
        'content-type': "application/json",
        'origin': "https://www.zillow.com",
        'referer': reference_url,
        'sec-ch-ua': generated_headers['sec-ch-ua'],
        'sec-ch-ua-mobile': generated_headers['sec-ch-ua-mobile'],
        'sec-ch-ua-platform': generated_headers['sec-ch-ua-platform'],
        'sec-fetch-dest': "empty",
        'sec-fetch-mode': "cors",
        'sec-fetch-site': "same-origin",
        'user-agent': generated_headers['user-agent']
    }
    if client_id:
        headers['client-id'] = client_id
    return headers

def is_element_in_viewport(driver, element):
    script = """
    var elem = arguments[0], box = elem.getBoundingClientRect(), cx = box.left + box.width / 2, cy = box.top + box.height / 2, e = document.elementFromPoint(cx, cy);
    for (; e; e = e.parentElement) {
        if (e === elem) return true;
    }
    return false;
    """
    return driver.execute_script(script, element)


def scroll_to_element(driver, element_id, max_attempts=10):
    """Scroll to an element until it is visible on the screen."""
    attempts = 0
    while attempts < max_attempts:
        try:
            element = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, element_id)))
            if is_element_in_viewport(driver, element):
                return # Element is in viewport.
            else:
                driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", element)
                random_delay(scrape_config['1s_delay'], scrape_config['2s_delay'])  # Random delay after scrolling.
        except Exception as e:
            print(f"Scrolling attempt {attempts + 1} failed: {e}")
        attempts += 1
        random_delay(scrape_config['1s_delay'], scrape_config['2s_delay'])  # Random delay before next attempt.

    if attempts >= max_attempts:
        print("Maximum scrolling attempts reached. The element might not be visible.")

def move_to_and_click(element, driver):
    """Move to an element before clicking to simulate mouse movement."""
    actions = ActionChains(driver)
    actions.move_to_element(element).pause(rd.uniform(0.5, 1.5)).click().perform()

def random_delay(min_delay=1, max_delay=3):
    """Wait for a random time between min_delay and max_delay seconds."""
    time.sleep(rd.uniform(min_delay, max_delay))

if __name__ == '__main__':
    print(what_is_my_ip())
