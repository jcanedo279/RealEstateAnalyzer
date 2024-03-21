
from zillowanalyzer.processors.real_estate_metrics_property_processor import real_estate_metrics_property_processing_pipeline
from zillowanalyzer.processors.home_features_processor import home_features_processing_pipeline
from zillowanalyzer.processors.alpha_beta_property_processor import alpha_beta_property_processing_pipeline



if __name__ == '__main__':
    real_estate_metrics_property_processing_pipeline()
    home_features_processing_pipeline()
    alpha_beta_property_processing_pipeline()
