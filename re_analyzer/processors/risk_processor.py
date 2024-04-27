import pandas as pd
import sys

from re_analyzer.analyzers.iterator import property_details_iterator, get_property_info_from_property_details


cnt = 0
property_details_list = []
for property_details in property_details_iterator():
    property_info = get_property_info_from_property_details(property_details)
    if 'zestimateHistory' not in property_details:
        continue
    zestimate_history = property_details['zestimateHistory']
    if len(zestimate_history) < 3:
        continue
    property_details_list.append({
        'zpid': property_info['zpid'],
        'zestimate_history': zestimate_history
    })

    cnt += 1
    if cnt > 7500:
        break


# Initialize an empty list to collect DataFrame rows
rows_list = []
for property_details in property_details_list:
    zpid = property_details['zpid']
    for record in property_details['zestimate_history']:
        # Append each record as a dictionary, including the zpid
        rows_list.append({
            'zpid': zpid,
            'Date': pd.to_datetime(record['Date']),
            'Price': record['Price']
        })

# Convert the list of dictionaries to a DataFrame
df = pd.DataFrame(rows_list)

# Set the DataFrame to use a MultiIndex of zpid and Date
df.set_index(['zpid', 'Date'], inplace=True)

# Sort the index for efficient slicing/querying
df.sort_index(inplace=True)

print(df)
