import os
import json
import numpy as np
import pandas as pd

from re_analyzer.processors.re_metrics_processor import real_estate_metrics_property_processing_pipeline
from re_analyzer.processors.home_features_processor import home_features_processing_pipeline
from re_analyzer.processors.zestimate_history_processor import save_zestimate_history_pipeline
from re_analyzer.utility.utility import PROPERTY_DATA_PATH


PROPERTY_STATIC_DF_PATH = os.path.join(PROPERTY_DATA_PATH, 'property_static_df.parquet')


def save_property_static_df(property_simple_metrics_df, property_features_df):
    property_static_df = property_simple_metrics_df.set_index('zpid')

    # Load from home features.
    property_static_df['home_features_score'] = property_features_df['home_features_score'].astype(float)
    property_static_df['is_waterfront'] = property_features_df['waterView_None'].apply(lambda x: 'False' if x == 1 else 'True')

    # Convert the string objects.
    property_static_df['street_address'] = property_static_df['street_address'].astype(str)
    property_static_df['image_url'] = property_static_df['image_url'].astype(str)
    property_static_df['property_url'] = property_static_df['property_url'].astype(str)

    property_static_df.to_parquet(PROPERTY_STATIC_DF_PATH)
    property_static_df.to_csv(os.path.join(PROPERTY_DATA_PATH, 'property_static_df.csv'))


if __name__ == '__main__':
    property_simple_metrics_df = real_estate_metrics_property_processing_pipeline()
    property_features_df = home_features_processing_pipeline()
    save_property_static_df(property_simple_metrics_df, property_features_df)
    save_zestimate_history_pipeline()
