import os
import sys
import time
import json
import configparser
import random as rd
import pandas as pd
from dotenv import load_dotenv
from dateutil.parser import parse
from enum import Enum, auto
from datetime import datetime


# Get the directory of the current script
SCRIPT_PATH = os.path.realpath(__file__)
ZILLOW_ANALYZER_PATH = '/'.join( SCRIPT_PATH.split('/')[:-2] )

def get_abs_path(rel_path):
    return os.path.join(ZILLOW_ANALYZER_PATH, rel_path)
CONFIG_PATH = get_abs_path('utility/project_config.cfg')


###############################
## DIRECTORY UTILITY METHODS ##
###############################

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


#############################
## GENERAL UTILITY METHODS ##
#############################

def random_delay(min_delay=1, max_delay=3):
    """Wait for a random time between min_delay and max_delay seconds."""
    time.sleep(rd.uniform(min_delay, max_delay))

def batch_generator(data, batch_size):
    """A generator to yield batches of data."""
    batch = []
    for item in data:
        batch.append(item)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch

def backoff_strategy(attempt, max_attempts=20):
    """
    Implements an exponential backoff strategy.
    :param attempt: Current attempt number.
    :param max_attempts: Maximum number of attempts before giving up.
    :return: True if should continue, False otherwise.
    """
    if attempt > max_attempts:
        print("Reached maximum attempt limit. Skipping...")
        return False
    
    wait_time = min(2 ** attempt, 120)  # Exponential backoff with a maximum wait
    print(f"Backoff activated: Waiting for {wait_time} seconds before retrying...")
    time.sleep(wait_time)
    return True


#############################
## PARSING UTILITY METHODS ##
#############################

def convert_to_enum(enum_type, value):
    for enum_member in enum_type:
        if enum_member.name == value:
            return enum_member
    raise ValueError(f"Invalid enum value: {value}")

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

def is_within_cooldown_period(last_checked_str, cooldown_window):
    if not last_checked_str:
        # If the meatadata does not hve a last_checked string, its not within the search cooldown.
        return False
    last_checked = datetime.fromisoformat(last_checked_str)
    return datetime.now() - last_checked < cooldown_window


############################
## PROJECT CONFIG MANAGER ##
############################

class SortListingBy(Enum):
    PRICE_DESC = auto()
    PRICE_ASC = auto()
    NEWEST = auto()
SORT_LISTING_BY_ENUM_TO_STRING = {
    SortListingBy.PRICE_DESC: "priced",
    SortListingBy.PRICE_ASC: "pricea",
    SortListingBy.NEWEST: "days"
}

class ProjectConfigManager:
    def __init__(self):
        self.config_dict = {}
        self.enum_types = [SortListingBy]
        self.load_config()

    def load_config(self):
        config = configparser.ConfigParser()
        config.read(CONFIG_PATH)
        print("Current working directory:", os.getcwd())
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

    def __setitem__(self, key, value):
        self.config_dict[key] = value


################################
## LOAD ENVIRONMENT VARIABLES ##
################################

load_dotenv()
SCRAPEOPS_API_KEY = os.environ.get('SCRAPE_OPS_API_KEY')
# if not SCRAPEOPS_API_KEY:
#     sys.exit("A SCRAPEOPS_API_KEY is required to generate plausible headers when scraping :<")


#######################
## LOAD LOCAL CONFIG ##
#######################

PROJECT_CONFIG = ProjectConfigManager()

DATA_PATH = get_abs_path('Data')

VISUAL_DATA_PATH = get_abs_path('Data/VisualData')
SEARCH_LISTINGS_DATA_PATH = get_abs_path('Data/SearchResults')
SEARCH_LISTINGS_METADATA_PATH = get_abs_path('Data/SearchResultsMetadata')
PROPERTY_DETAILS_PATH = get_abs_path('Data/PropertyDetails')
SEARCH_RESULTS_PROCESSED_PATH = get_abs_path('Data/search_listings.csv')

ALPHA_BETA_DATA_PATH = get_abs_path('Data/PropertyData/alpha_beta_data.csv')
REAL_ESTATE_METRICS_DATA_PATH = get_abs_path('Data/PropertyData/real_estate_metrics_data.csv')
HOME_FEATURES_DATAFRAME_PATH = get_abs_path('Data/PropertyData/home_features_df.parquet')
