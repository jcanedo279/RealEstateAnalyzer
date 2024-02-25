import pandas as pd
from sklearn.preprocessing import StandardScaler

from zillowanalyzer.scrapers.scraping_utility import DATA_PATH

def load_data():
    alpha_beta_df = pd.read_csv(f'{DATA_PATH}/AlphaBetaStats.csv').drop(['zip_code'], axis=1)
    property_metrics_df = pd.read_csv(f'{DATA_PATH}/processed_property_metric_results.csv').drop(['zip_code', 'street_address'], axis=1)
    combined_df = pd.merge(alpha_beta_df, property_metrics_df, on='zpid', how='inner').drop(['zpid'], axis=1)
    return combined_df.columns, combined_df

def preprocess_data(combined_df, features, return_scaler=False):
    # Filter out outliers based on IQR
    Q1 = combined_df[features].quantile(0.25)
    Q3 = combined_df[features].quantile(0.75)
    IQR = Q3 - Q1
    filtered_df = combined_df[~((combined_df[features] < (Q1 - 1.5 * IQR)) | (combined_df[features] > (Q3 + 1.5 * IQR))).any(axis=1)]
    # Fit the scaler and transform the data
    scaler = StandardScaler()
    scaled_data = scaler.fit_transform(filtered_df[features])
    scaled_df = pd.DataFrame(scaled_data, columns=features)
    if return_scaler:
        return scaled_df, filtered_df, scaler
    return scaled_df, filtered_df


