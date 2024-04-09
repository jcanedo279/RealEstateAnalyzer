import glob
import os
import json
from collections import defaultdict

from zillowanalyzer.utility.utility import PROPERTY_DETAILS_PATH, DATA_PATH, save_json


def aggregate_options(data, options_dict, parent_key=''):
    """
    Recursively aggregate options for each key in the nested dictionary.
    """
    if isinstance(data, dict):
        for key, value in data.items():
            # Construct a new key that represents the path through the nested structure
            new_key = f"{parent_key}.{key}" if parent_key else key
            aggregate_options(value, options_dict, new_key)
    elif isinstance(data, list):
        # Assume lists contain simple values or dictionaries
        key = parent_key
        for item in data:
            if isinstance(item, dict):
                aggregate_options(item, options_dict, parent_key)
            else:
                # Directly add simple values from lists to the options set
                options_dict[key].add(item)
    else:
        # Base case: data is a simple value, add it to the options set
        options_dict[parent_key].add(data)


# These are all resoFacts features/keys.
bool_features = {'canRaiseHorses', 'furnished', 'hasAssociation', 'hasAttachedGarage', 'hasAttachedProperty', 'hasCooling', 'hasCarport', 'hasElectricOnProperty', 'hasFireplace', 'hasGarage', 'hasHeating', 'hasOpenParking', 'hasPrivatePool', 'hasView', 'hasWaterfrontView', 'isSeniorCommunity', 'hasAdditionalParcels', 'hasPetsAllowed', 'isNewConstruction'}
container_features = {'accessibilityFeatures', 'associationAmenities', 'appliances', 'communityFeatures', 'cooling', 'doorFeatures', 'electric', 'fireplaceFeatures', 'flooring', 'greenEnergyEfficient', 'heating', 'horseAmenities', 'interiorFeatures', 'laundryFeatures', 'patioAndPorchFeatures', 'poolFeatures', 'roadSurfaceType', 'securityFeatures', 'sewer', 'spaFeatures', 'utilities', 'waterSource', 'waterfrontFeatures', 'windowFeatures', 'buildingFeatures', 'otherStructures'}
# Broken down with delimeters ',' and '/'
composite_categorical_features_to_delimeter = {'waterView', 'roofType', 'fencing'}
categorical_features = {'basement', 'commonWalls', 'architecturalStyle'}

def process_reso_facts_features(base_path=PROPERTY_DETAILS_PATH):
    options_dict = defaultdict(set)
    total_props = 0

    # Loop through each zip code.
    total_props = 0
    for zip_code_folder in glob.glob(os.path.join(base_path, '*')):
        # Process each JSON file within the zip code folder (each property).
        total_props += len(glob.glob(os.path.join(zip_code_folder, '*_property_details.json')))
        for json_file_path in glob.glob(os.path.join(zip_code_folder, '*_property_details.json')):
            with open(json_file_path, 'r') as json_file:
                property_details = json.load(json_file)
                if 'props' not in property_details:
                    continue
                property_data = property_details['props']['pageProps']['componentProps']['gdpClientCache']
                first_key = next(iter(property_data))
                property_info = property_data[first_key].get("property", None)
                purchase_price = property_info.get('price', 0)
                reso_facts = property_info.get("resoFacts", {})
                total_props += 1

                # save_json(reso_facts, f'reso_facts_{rd.random()}.json')

                aggregate_options(reso_facts, options_dict)

    return options_dict, total_props

# Assuming PROPERTY_DETAILS_PATH is defined and points to your data directory
options_dict, total_props = process_reso_facts_features(PROPERTY_DETAILS_PATH)

# Example of printing the collected options for each parameter
# for key, options in options_dict.items():
#     print(f"{key}: {options}")
# print(f"Total properties processed: {total_props}")

for key, option in options_dict.items():
    options_dict[key] = list(option)

# Example of printing the collected options for each parameter
save_json(options_dict, os.path.join(DATA_PATH, "Examples", "all_reso_facts.json"))

