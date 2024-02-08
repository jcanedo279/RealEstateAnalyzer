import os
import time
import json
import re
import configparser
import random as rd
from enum import Enum, auto
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from fake_useragent import UserAgent
from functools import wraps
from contextlib import contextmanager
from selenium.webdriver.chrome.options import Options

from proxy_scraper import ProxyManager


CONFIG_PATH = 'scrape_config.cfg'
USER_AGENT = UserAgent()

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

# Fetch the ScrapeConfig to use it for the rest of the utility file.
scrape_config = ScrapeConfigManager()
proxy_manager = ProxyManager()


def get_driver(headless=False):
    options = Options()
    if headless:
        options.add_argument("--headless")
    options.add_argument('--ignore-ssl-errors=yes')
    options.add_argument('--ignore-certificate-errors')
    options.add_argument(f"--window-size={int(rd.uniform(700, 1400))},{rd.uniform(400,900)}")
    options.add_argument("--disable-dev-shm-usage")
    # options.add_argument(f'--proxy-server={proxy_wrapper.proxy_string}')



    # Test
    options.add_argument("enable-automation")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-extensions")
    options.add_argument("--dns-prefetch-disable")
    options.add_argument("--disable-gpu")

    return uc.Chrome(options=options)

@contextmanager
def get_selenium_driver(url, with_proxy=True, headless=False):
    # proxy_wrapper = proxy_manager.get_proxy_wrapper()
    driver = get_driver(headless=headless)
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
                    time.sleep(scrape_config['request_retry_delay'])
            time.sleep(scrape_config['request_fail_delay'])
            return None
        return wrapper
    return decorator


@retry_request(scrape_config)
def extract_cookies_from_driver(driver, delay):
    time.sleep(delay)  # Wait for cookies to be created.
    cookies = driver.get_cookies()
    session_cookies = {cookie['name']: cookie['value'] for cookie in cookies}
    return "; ".join([f"{name}={value}" for name, value in session_cookies.items()])

@retry_request(scrape_config)
def extract_region_data_from_driver(driver, delay):
    time.sleep(delay)
    soup = BeautifulSoup(driver.page_source, 'html.parser')
    script_tag = soup.find('script', string=re.compile('regionSelection'))
    if script_tag:
        script_content = script_tag.string
        return json.loads(script_content)['props']['pageProps']['searchPageState']['queryState']
    return None

@retry_request(scrape_config)
def extract_property_details_from_driver(driver, delay):
    print('value loaded')
    time.sleep(delay)
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
        

# print(what_is_my_ip())
