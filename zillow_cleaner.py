import shutil
import os

directory_path = 'HomeData/'

if os.path.exists(directory_path):
    shutil.rmtree(directory_path)
    print(f"'{directory_path}' has been deleted.")
else:
    print(f"No directory found at '{directory_path}'.")

os.remove('calculated_ratios_all_zip_codes.json')
