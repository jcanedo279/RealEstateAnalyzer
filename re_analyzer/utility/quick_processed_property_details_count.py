import pandas as pd

from re_analyzer.utility.utility import REAL_ESTATE_METRICS_DATA_PATH


# Read the CSV file
df = pd.read_csv(REAL_ESTATE_METRICS_DATA_PATH)

# Count unique homes by 'zpid'
unique_homes_count = df['zpid'].nunique()

print(f"Unique homes count: {unique_homes_count}")
print(f"Total homes count: {len(df)}")
