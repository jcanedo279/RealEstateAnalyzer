import os
import shutil
import time
import json
import re
import glob
import platform
import subprocess
import random as rd
from bs4 import BeautifulSoup
import undetected_chromedriver as uc
from functools import wraps
from contextlib import contextmanager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from collections import defaultdict
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys

from zillowanalyzer.utility.utility import PROJECT_CONFIG, DATA_PATH, SEARCH_LISTINGS_METADATA_PATH, random_delay


# Chromium versions found at: https://vikyd.github.io/download-chromium-history-version/#/
CHROME_BINARY_EXECUTABLE_PATH = os.environ.get('CHROME_BINARY_EXECUTABLE_PATH')
# Use chrome://version/ to locate the user_data_dir path.
CHROME_USER_DATA_DIR = os.environ.get('CHROME_USER_DATA_DIR')
local_path_exists = os.path.exists(CHROME_USER_DATA_DIR)

MUNICIPALITIES_DATA_PATH = os.path.join(DATA_PATH, 'florida_municipalities_data.txt')


class ZillowChromeDriver(uc.Chrome):
    def __init__(self, *args, ignore_detection=False, **kwargs):
        self.ignore_detection = ignore_detection
        super().__init__(*args, **kwargs)
    
    def get(self, url):
        super().get(url)
        if self.ignore_detection:
            return
        # If a Captcha is detected, stop scraping.
        try:
            is_first_attempt = True
            while self.find_element(By.ID, 'px-captcha-wrapper'):
                if is_first_attempt:
                    print('\n')
                print("Pls halp, I've been bad 🥺👉👈", end='\r')
                time.sleep(10)
                is_first_attempt = False
        except:
            pass

class ChromeProfileManager():
    min_profile_number = PROJECT_CONFIG['min_profile_number']
    max_profile_number = PROJECT_CONFIG['max_profile_number']
    def __init__(self):
        # We start with a random profile so that we do not become more identifiable. Starting to request from the same profile is temporarily detectable.
        self.current_profile_number = self.next_profile_number(random_profile=True)

    def next_profile_number(self, random_profile=False):
        if random_profile:
            self.current_profile_number = rd.randint(self.min_profile_number, self.max_profile_number)
        else:
            self.current_profile_number = self.min_profile_number + (self.current_profile_number - self.min_profile_number + 1) % (self.max_profile_number - self.min_profile_number + 1)
        return self.current_profile_number

PROFILE_CACHE_FILES = ['Cookies', 'Cookies-journal', 'History', 'History-journal', 'Visited Links', 'Web Data', 'Web Data-journal', 'Local Storage', 'Session Storage', 'Sessions', 'IndexedDB', 'GPUCache']
chromeProfileManager = ChromeProfileManager()
def clean_profile_data(profile_number):
    for cache_file in PROFILE_CACHE_FILES:
        profile_cache_path = os.path.join(CHROME_USER_DATA_DIR, f"Profile {profile_number}", cache_file)
        if os.path.exists(profile_cache_path):
            if os.path.isfile(profile_cache_path):
                os.remove(profile_cache_path)
            elif os.path.isdir(profile_cache_path):
                shutil.rmtree(profile_cache_path)

def kill_chrome_leaks(kill_unix_leaks=False):
    if platform.system() == "Windows":
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "chrome_for_testing.exe"],
                check=True,
                stdout=subprocess.DEVNULL,  # Suppress standard output
                stderr=subprocess.DEVNULL   # Suppress errors
            )
        except subprocess.CalledProcessError as e:
            pass
    elif kill_unix_leaks:
        try:
            subprocess.run(
                ["pkill", "-f", "chrome_for_testing"],
                check=True,
                stdout=subprocess.DEVNULL,  # Suppress standard output
                stderr=subprocess.DEVNULL   # Suppress errors
            )
        except subprocess.CalledProcessError as e:
            pass

def get_chrome_options(headless=False, random_profile=False):
    options = uc.ChromeOptions()
    if headless:
        options.add_argument("--headless")
        # The following options help mitigate the detectability of headless browsers.
        options.add_argument("--disable-setuid-sandbox")
    if local_path_exists:
        profile_number = chromeProfileManager.next_profile_number(random_profile=random_profile)
        PROJECT_CONFIG['profile_number'] = profile_number
        profile_directory = f"Profile {profile_number}"
        options.add_argument(f"--user-data-dir={CHROME_USER_DATA_DIR}")
        options.add_argument(f"--profile-directory={profile_directory}")
    options.add_argument('--ignore-ssl-errors=yes')
    options.add_argument('--ignore-certificate-errors')
    options.add_argument(f"--window-size={int(rd.uniform(700, 1400))},{rd.uniform(400,900)}")
    options.add_argument("--disable-dev-shm-usage")
    return options

@contextmanager
def get_selenium_driver(url, headless=False, ignore_detection=False, random_profile=False, clean_profile=False):
    driver = None
    try:
        options = get_chrome_options(headless=headless, random_profile=random_profile)
        if clean_profile:
            clean_profile_data(PROJECT_CONFIG['profile_number'])
        driver = ZillowChromeDriver(options=options, browser_executable_path=CHROME_BINARY_EXECUTABLE_PATH, ignore_detection=ignore_detection, no_sandbox=False, use_subprocess=False)
        driver.get(url)
        yield driver
    except Exception as e:
        print(f"An error occurred in the driver context: {e}")
        raise
    finally:
        if driver:
            driver.quit()

def retry_request(project_config):
    max_attempts = project_config['max_reconnect_retries']
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempts in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempts >= max_attempts:
                        break
                    random_delay(5, 10)
            random_delay(15, 30)
            return None
        return wrapper
    return decorator


@retry_request(PROJECT_CONFIG)
def extract_cookies_from_driver(driver, delay):
    random_delay(delay, 2*delay)
    cookies = driver.get_cookies()
    session_cookies = {cookie['name']: cookie['value'] for cookie in cookies}
    return "; ".join([f"{name}={value}" for name, value in session_cookies.items()])


def is_element_in_viewport(driver, element):
    script = """
    var elem = arguments[0], box = elem.getBoundingClientRect(), cx = box.left + box.width / 2, cy = box.top + box.height / 2, e = document.elementFromPoint(cx, cy);
    for (; e; e = e.parentElement) {
        if (e === elem) return true;
    }
    return false;
    """
    return driver.execute_script(script, element)


def scroll_to_element(driver, container_selector, element_selector, max_attempts=10):
    """Scroll to an element until it is visible on the screen."""
    attempts = 0
    try:
        container = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, container_selector)))
        element = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, element_selector)))
        move_to_and_click(container, driver)
    except Exception as e:
        print(f"Either the container or element could not be located.")
    while attempts < max_attempts:
        if is_element_in_viewport(driver, element):
            return # Element is in viewport.
        else:
            ActionChains(driver).send_keys(Keys.PAGE_DOWN).perform()
            # random_delay(0, 0.025)  # Random delay after scrolling.
        attempts += 1
        random_delay(1, 2)  # Random delay before next attempt to scroll.

    if attempts >= max_attempts:
        print("Maximum scrolling attempts reached. The element might not be visible.")

def offscreen_click(element, driver):
    driver.execute_script("arguments[0].click();", element)

def move_to_and_click(element, driver, and_hold=False):
    """Move to an element before clicking to simulate mouse movement."""
    actions = ActionChains(driver)
    if and_hold:
        actions.move_to_element(element).pause(rd.uniform(0.1, 0.5)).click_and_hold(on_element=element).perform()
    else:
        actions.move_to_element(element).pause(rd.uniform(0.1, 0.5)).click().perform()


def load_search_metadata():
    municipality_to_zpids = defaultdict(set)
    for metadata_path in glob.glob(os.path.join(SEARCH_LISTINGS_METADATA_PATH, "*_metadata.json")):
        municipality = os.path.basename(metadata_path).split("_metadata.json")[0]
        with open(metadata_path, "r") as file:
            metadata = json.load(file)
            municipality_to_zpids[municipality].update(metadata.get('zpids', []))
    return municipality_to_zpids

def load_search_municipalities():
    # A dictionary to hold the mapping of counties to their municipalities
    county_to_municipalities = defaultdict(list)

    with open(MUNICIPALITIES_DATA_PATH, 'r') as file:
        for line in file:
            # Using regex to split the line by tabs or multiple spaces
            fields = re.split(r'\t+', line.strip())
            
            # Adjust the index based on the actual structure if needed
            municipality = fields[1].strip()
            county = fields[2].strip()

            # Remove the † symbol if present
            municipality_cleaned = municipality.replace("†", "").replace("-", " ")
            county_to_municipalities[county].append(municipality_cleaned)
    return county_to_municipalities

def what_is_my_ip():
    what_is_my_ip_url = "http://httpbin.org/ip"
    with get_selenium_driver(what_is_my_ip_url) as driver:
        time.sleep(1)
        soup = BeautifulSoup(driver.page_source, 'html.parser')
        try:
            pre_tag_content = soup.find('pre').text
            json_data = json.loads(pre_tag_content)
            ip_address_text = json_data["origin"]
            ip_addresses = [ip.strip() for ip in ip_address_text.split(',')]
            return ip_addresses
        except:
            return



if __name__ == '__main__':
    print(what_is_my_ip())
    kill_chrome_leaks()
