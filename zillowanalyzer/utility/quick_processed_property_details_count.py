import pandas as pd


file_path = 'zillowanalyzer/Data/processed_property_metric_results.csv'

# Read the CSV file
df = pd.read_csv(file_path)

# Count unique homes by 'zpid'
unique_homes_count = df['zpid'].nunique()

print(f"Unique homes count: {unique_homes_count}")
print(f"Total homes count: {len(df)}")
