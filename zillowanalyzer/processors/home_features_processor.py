import os
import pandas as pd
import matplotlib.pyplot as plt
import re
import shap
import numpy as np
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_squared_error

from zillowanalyzer.utility.utility import DATA_PATH, VISUAL_DATA_PATH, ensure_directory_exists
from zillowanalyzer.analyzers.iterator import property_details_iterator, get_property_info_from_property_details
from zillowanalyzer.analyzers.correlatory_data_analysis import visualize_pairwise_correlation, visualize_pairwise_distribution

# Define feature categories
FEATURE_CATEGORIES = {
    'bool_features': {'canRaiseHorses', 'furnished', 'hasAssociation', 'hasAttachedGarage', 'hasAttachedProperty', 'hasCooling', 'hasCarport', 'hasElectricOnProperty', 'hasFireplace', 'hasGarage', 'hasHeating', 'hasOpenParking', 'hasPrivatePool', 'hasView', 'hasWaterfrontView', 'isSeniorCommunity', 'hasAdditionalParcels', 'hasPetsAllowed', 'isNewConstruction'},
    'container_features': {'accessibilityFeatures', 'associationAmenities', 'appliances', 'communityFeatures', 'cooling', 'doorFeatures', 'electric', 'fireplaceFeatures', 'flooring', 'greenEnergyEfficient', 'heating', 'horseAmenities', 'interiorFeatures', 'laundryFeatures', 'patioAndPorchFeatures', 'poolFeatures', 'roadSurfaceType', 'securityFeatures', 'sewer', 'spaFeatures', 'utilities', 'waterSource', 'waterfrontFeatures', 'windowFeatures', 'buildingFeatures', 'otherStructures'},
    'composite_container_features': {'waterView', 'roofType', 'fencing'},
    'categorical_features': {'basement', 'commonWalls', 'architecturalStyle'}
}
MIN_THRESHOLD = 1000

# Create a directory for plots if it doesn't exist
bool_home_features = os.path.join(VISUAL_DATA_PATH, "home_features", "bool_features")
cat_home_features = os.path.join(VISUAL_DATA_PATH, "home_features", "cat_features")
ensure_directory_exists(bool_home_features)
ensure_directory_exists(cat_home_features)


def aggregate_features_from_json(property_info, all_features):
    zpid = property_info.get('zpid', 0)
    purchase_price = property_info.get('price', 0)
    
    features_dict = all_features.setdefault(zpid, {'purchase_price': purchase_price})
    reso_facts = property_info.get("resoFacts", {})
    
    for feature, value in reso_facts.items():
        process_feature_value(feature, value, features_dict)

def process_feature_value(feature, value, features_dict):
    if feature in FEATURE_CATEGORIES['bool_features']:
        features_dict[feature] = True if value else False
    elif feature in FEATURE_CATEGORIES['composite_container_features']:
        for item in re.split('[,\/]', str(value)):
            features_dict[f'{feature}_{item}'] = 1
    elif feature in FEATURE_CATEGORIES['container_features'] or feature in FEATURE_CATEGORIES['categorical_features']:
        if isinstance(value, list):
            for item in value:
                features_dict[f'{feature}_{item}'] = 1
        else:
            features_dict[f'{feature}_{value}'] = 1

def load_and_aggregate_features():
    all_features = {}
    for property_details in property_details_iterator():
        property_info = get_property_info_from_property_details(property_details)
        aggregate_features_from_json(property_info, all_features)
    return pd.DataFrame.from_dict(all_features, orient='index').fillna(0)

def filter_features_by_threshold(features_df, min_threshold=MIN_THRESHOLD):
    # Convert boolean columns to numerical (0s and 1s) if not already done
    for col in FEATURE_CATEGORIES['bool_features']:
        if col in features_df.columns and features_df[col].dtype == 'object':
            features_df[col] = features_df[col].astype(int)

    # Drop features that don't meet the minimum occurrence threshold
    feature_occurrences = features_df.drop(columns=["purchase_price"], errors='ignore').sum()
    features_above_threshold = feature_occurrences[feature_occurrences >= min_threshold].index
    
    # Group columns by base feature name (before the first underscore)
    base_feature_groups = {}
    for column in features_above_threshold:
        base_feature = column.split('_')[0]
        if base_feature not in base_feature_groups:
            base_feature_groups[base_feature] = []
        base_feature_groups[base_feature].append(column)
    
    # Initialize columns to keep
    columns_to_keep = ["purchase_price"]
    
    # Filter out groups with only one column, unless they are boolean features
    for base_feature, columns in base_feature_groups.items():
        if len(columns) > 1:
            columns_to_keep.extend(columns)
        elif base_feature in FEATURE_CATEGORIES['bool_features']:
            # For boolean features, check if there's a minimum number of False instances
            if (features_df[base_feature] == 0).sum() >= MIN_THRESHOLD:
                columns_to_keep.append(base_feature)
    
    # Return the filtered DataFrame
    return features_df[columns_to_keep]

def apply_XGB_on_home_features(features_df, predictor):
    # Prepare the data
    X = features_df.drop(columns=[predictor], errors='ignore')
    y = features_df[predictor]

    # Split the data into training and testing sets
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    # Initialize an XGBoost regressor
    model = xgb.XGBRegressor(objective='reg:squarederror', n_estimators=100, learning_rate=0.1, max_depth=5, random_state=42, enable_categorical=True)
    # Fit the model
    model.fit(X_train, y_train)
    # Make predictions
    y_pred = model.predict(X_test)

    # Calculate the mean squared error
    mse = mean_squared_error(y_test, y_pred)
    print(f"Mean Squared Error: {mse}")
    return model

def add_shap_for_home_features(model, features_df, predictor):
    X = features_df.drop(predictor, axis=1)
    # Calculate SHAP values
    explainer = shap.Explainer(model)
    shap_values = explainer(X)
    # Aggregate SHAP values for each observation
    cumulative_shap_scores = shap_values.values.sum(axis=1)
    # Normalize SHAP values by the sum of absolute values for comparison (optional)
    normalized_cumulative_shap_scores = cumulative_shap_scores / np.abs(shap_values.values).sum(axis=1)
    features_df['home_features_score'] = normalized_cumulative_shap_scores

    mean_shap_values = np.abs(shap_values.values).mean(axis=0)
    shap_correlation_df = pd.DataFrame({
        'Feature': X.columns,
        'SHAP_Correlation_Score': mean_shap_values / np.abs(mean_shap_values).sum(axis=0)
    })
    shap_correlation_df.sort_values('SHAP_Correlation_Score', ascending=False, inplace=True)
    return shap_correlation_df


def plot_feature_histograms(features_df):
    # Get base feature names for container and categorical features
    container_and_categorical_features = set(FEATURE_CATEGORIES['container_features']).union(FEATURE_CATEGORIES['composite_container_features'], FEATURE_CATEGORIES['categorical_features'])

    for base_feature in container_and_categorical_features:
        # Filter columns that start with the base feature name
        feature_columns = [col for col in features_df.columns if col.startswith(f"{base_feature}_")]
        if not feature_columns:
            continue

        # Sum occurrences for each category within the base feature
        category_sums = features_df[feature_columns].sum()

        # Prepare data for plotting: Match category sums with their extracted names
        category_names = [col.split('_', 1)[1] for col in feature_columns]
        category_sums.index = category_names

        # Plot the histogram for the base feature categories
        plt.figure(figsize=(10, 6))
        category_sums.plot(kind='bar')
        plt.xlabel('Category')
        plt.ylabel('Count')
        plt.title(f'Histogram of {base_feature} Categories')
        plt.xticks(rotation=45, ha="right")
        plt.tight_layout()
        plt.savefig(os.path.join(cat_home_features, f'{base_feature}_categories_histogram.png'))
        plt.close()

    # Plot for boolean features separately since they don't need grouping
    for bool_feature in FEATURE_CATEGORIES['bool_features']:
        if bool_feature in features_df.columns:
            # Boolean features are directly plotted since they don't have sub-categories
            plt.figure(figsize=(10, 6))
            features_df[bool_feature].value_counts().plot(kind='bar')
            plt.xlabel('Value')
            plt.ylabel('Count')
            plt.title(f'Histogram of {bool_feature}')
            plt.xticks(rotation=0)
            plt.tight_layout()
            plt.savefig(os.path.join(bool_home_features, f'{bool_feature}_histogram.png'))
            plt.close()




def main():
    features_df = load_and_aggregate_features()
    features_df = filter_features_by_threshold(features_df)
    plot_feature_histograms(features_df.drop(columns=['purchase_price'], errors='ignore'))

    predictor = 'purchase_price'
    model = apply_XGB_on_home_features(features_df, predictor)
    shap_correlation_df = add_shap_for_home_features(model, features_df, predictor)
    print(shap_correlation_df)

    home_features_df = features_df[['home_features_score', 'purchase_price']]
    # Save to Parquet (efficient and preserves data types well)
    home_features_df.to_parquet(f'{DATA_PATH}/SavedDataframes/home_features_df.parquet')

    visualize_pairwise_correlation(home_features_df, path=f"{VISUAL_DATA_PATH}/correlatory/home_features_pairwise_correlation.png", title="Home Features vs. Price Correlation (SHAP)")
    visualize_pairwise_distribution(home_features_df, path=f"{VISUAL_DATA_PATH}/correlatory/home_features_pairwise_distribution.png", title="Home Features vs. Price Distribution (SHAP)")

if __name__ == "__main__":
    main()
