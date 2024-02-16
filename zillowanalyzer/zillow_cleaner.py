import shutil
import os

from zillowanalyzer.scrapers.scraping_utility import *

directory_path = HOME_DATA_PATH

if os.path.exists(directory_path):
    shutil.rmtree(directory_path)
    print(f"'{directory_path}' has been deleted.")
else:
    print(f"No directory found at '{directory_path}'.")

os.remove('calculated_ratios_all_zip_codes.json')
