import os
import pandas as pd
from re_analyzer.utility.utility import PROPERTY_DATA_PATH
from re_analyzer.analyzers.iterator import property_details_iterator, get_property_info_from_property_details


ZESTIMATE_HISTORY_PARQUET_PATH = os.path.join(PROPERTY_DATA_PATH, 'zestimate_history_df.parquet')
ZESTIMATE_HISTORY_CSV_PATH = os.path.join(PROPERTY_DATA_PATH, 'zestimate_history_df.csv')


def save_zestimate_history(zestimate_history_data, parquet_path, csv_path):
    # Convert the zestimate history data to a DataFrame
    records = []
    for zpid, history in zestimate_history_data.items():
        for entry in history:
            records.append({
                'zpid': zpid,
                'Date': entry['Date'],
                'Price': entry['Price']
            })
    zestimate_history_df = pd.DataFrame(records)
    # Ensure 'Date' column is in datetime format
    zestimate_history_df['Date'] = pd.to_datetime(zestimate_history_df['Date'])
    # Sort the DataFrame by 'Date'
    zestimate_history_df.sort_values(by='Date', inplace=True)
    # Set 'Date' as the index
    zestimate_history_df.set_index('Date', inplace=True)
    
    # Save the DataFrame to a Parquet file
    zestimate_history_df.to_parquet(parquet_path, compression='gzip')
    
    # Save the DataFrame to a CSV file
    zestimate_history_df.to_csv(csv_path)
    
    print(f"Zestimate history saved to {parquet_path} and {csv_path}")

def save_zestimate_history_pipeline():
    zestimate_history_data = {}
    total_properties = 0
    processed_properties = 0

    for property_details in property_details_iterator():
        total_properties += 1
        property_info = get_property_info_from_property_details(property_details)
        if not property_info:
            print(f"Skipping property with missing info: {property_details}")
            continue
        if 'zestimateHistory' not in property_details:
            print(f"Skipping property without zestimateHistory: {property_info}")
            continue
        zpid = property_info.get("zpid", 0)
        if zpid == 0:
            print(f"Skipping property with invalid zpid: {property_info}")
            continue
        zestimate_history = property_details['zestimateHistory']
        zestimate_history_data[zpid] = zestimate_history
        processed_properties += 1

    print(f"Total properties processed: {total_properties}")
    print(f"Properties with valid zestimate history: {processed_properties}")
    print(f"Properties missing zestimate history: {total_properties - processed_properties}")

    save_zestimate_history(zestimate_history_data, ZESTIMATE_HISTORY_PARQUET_PATH, ZESTIMATE_HISTORY_CSV_PATH)

if __name__ == "__main__":
    save_zestimate_history_pipeline()
