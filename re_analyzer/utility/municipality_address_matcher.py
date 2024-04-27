import os
import json
from collections import defaultdict
import difflib

from re_analyzer.utility.utility import SEARCH_LISTINGS_DATA_PATH

"""
================================================================================
Municipality Address Matcher
--------------------------------------------------------------------------------
This script iterates through a directory structure containing JSON files with 
property listing information. It extracts and maps municipalities to the cities 
mentioned in property addresses, identifying the closest match for each 
municipality based on the address cities available. It then analyzes the 
differences between the municipality names and the matched address cities, 
highlighting variations which could indicate discrepancies or normalization 
issues in the naming conventions used.

Purpose:
- To create a mapping of municipalities to their address cities.
- To identify and display the differences between municipality names and their 
  closest address city matches, aiding in data normalization efforts.

Usage:
Intended to be run as a standalone script.

Output:
- A dictionary mapping each municipality to its closest address city match.
- A dictionary highlighting the differences between municipality names and 
  their matched address cities, for further analysis and normalization.
================================================================================
"""

def load_search_results(search_results_dir):
    """Load search results from given directory into a mapping."""
    municipality_to_address_cities = defaultdict(set)

    for municipality in os.listdir(search_results_dir):
        municipality_dir = os.path.join(search_results_dir, municipality)
        if os.path.isdir(municipality_dir):
            for filename in os.listdir(municipality_dir):
                file_path = os.path.join(municipality_dir, filename)
                try:
                    with open(file_path, 'r') as file:
                        data = json.load(file)
                    for entry in data:
                        address_city = entry.get('addressCity')
                        if address_city:
                            municipality_to_address_cities[municipality].add(address_city)
                except (IOError, ValueError) as e:
                    print(f"Error reading or parsing file {file_path}: {e}")
    return municipality_to_address_cities

def find_closest_matches(municipality_to_address_cities):
    """Find the closest match for each municipality from its address cities."""
    final_mapping = {}
    for municipality, address_cities in municipality_to_address_cities.items():
        address_cities_list = list(address_cities)
        closest_matches = difflib.get_close_matches(municipality, address_cities_list, n=1, cutoff=0.6)
        final_mapping[municipality] = closest_matches[0] if closest_matches else None
    return final_mapping

def main():
    municipality_to_address_cities = load_search_results(SEARCH_LISTINGS_DATA_PATH)
    final_mapping = find_closest_matches(municipality_to_address_cities)

    # Identify and print municipalities where the name significantly differs from its address cities
    difference_mapping = {municipality: address_city for municipality, address_city in final_mapping.items() if address_city and municipality.lower() != address_city.lower()}
    print(difference_mapping)

if __name__ == "__main__":
    main()
