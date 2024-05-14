import os
import pandas as pd

from re_analyzer.processors.real_estate_metrics_property_processor import real_estate_metrics_property_processing_pipeline
from re_analyzer.processors.home_features_processor import home_features_processing_pipeline
from re_analyzer.processors.alpha_beta_property_processor import alpha_beta_property_processing_pipeline
from re_analyzer.utility.utility import PROPERTY_DATA_PATH


PROPERTY_DF_PATH = os.path.join(PROPERTY_DATA_PATH, 'property_df.parquet')

def save_property_df(metrics_df, features_df, timeseries_df):
    property_df = pd.merge(timeseries_df, metrics_df, on='zpid', how='inner').set_index('zpid')

    # Load from home features.
    property_df['home_features_score'] = features_df['home_features_score'].astype(float)
    property_df['is_waterfront'] = features_df['waterView_None'].apply(lambda x: 'False' if x == 1 else 'True')

    # Convert the string objects.
    property_df['street_address'] = property_df['street_address'].astype(str)
    property_df['image_url'] = property_df['image_url'].astype(str)
    property_df['property_url'] = property_df['property_url'].astype(str)

    property_df.to_parquet(PROPERTY_DF_PATH)

def load_property_df():
    property_df = pd.read_parquet(PROPERTY_DF_PATH)
    print(property_df.head())


if __name__ == '__main__':
    metrics_df = real_estate_metrics_property_processing_pipeline()
    features_df = home_features_processing_pipeline()
    timeseries_df = alpha_beta_property_processing_pipeline()
    save_property_df(metrics_df, features_df, timeseries_df)
