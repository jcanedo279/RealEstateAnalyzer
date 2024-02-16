import pandas as pd
import numpy as np
import seaborn as sns
import matplotlib.pyplot as plt
from statsmodels.stats.outliers_influence import variance_inflation_factor
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

from zillowanalyzer.scrapers.scraping_utility import *

# Load datasets
alpha_beta_df = pd.read_csv(f'{DATA_PATH}/AlphaBetaStats.csv')
alpha_beta_df.drop(['zip_code'], axis=1, inplace=True)
property_metrics_df = pd.read_json(f'{DATA_PATH}/processed_property_metric_results.json')
property_metrics_df.drop(['zip_code', 'street_address'], axis=1, inplace=True)

# Merge datasets on 'zpid'
combined_df = pd.merge(alpha_beta_df, property_metrics_df, on='zpid', how='inner')
combined_df.drop(['zpid'], axis=1, inplace=True)


def calculate_pairwise_correlation():
    # Calculate correlation matrix
    corr_matrix = combined_df.corr()

    # Plot heatmap
    plt.figure(figsize=(10, 8))
    sns.heatmap(corr_matrix, annot=True, cmap='coolwarm', cbar=False, fmt='.1f')
    plt.subplots_adjust(left=0.25, bottom=0, right=1, top=1)
    plt.show()
    plt.savefig(f'{VISUAL_DATA_PATH}/pairwise_correlation.png', bbox_inches='tight', dpi=300)


def calculate_pairwise_distribution():
    # Plot pairwise relationships
    sns.pairplot(combined_df, diag_kind='kde')
    plt.savefig(f'{VISUAL_DATA_PATH}/pairwise_distribution.png')


def calculate_multicolinearity():
    # Function to calculate VIF
    def calculate_vif(df):
        vif_data = pd.DataFrame()
        vif_data["feature"] = df.columns
        
        # Calculating VIF for each feature
        vif_data["VIF"] = [variance_inflation_factor(df.values, i) for i in range(len(df.columns))]
        return vif_data

    # Assuming `combined_df` is your DataFrame after preprocessing
    # Drop non-numeric or identifier columns before VIF calculation
    numeric_df = combined_df.select_dtypes(include=[np.number])
    vif_dataframe = calculate_vif(numeric_df)
    print(vif_dataframe)


def calculate_PCA_for_metrics():
    print(combined_df.shape)
    # Standardize the data
    scaler = StandardScaler()
    df_scaled = scaler.fit_transform(combined_df)

    # Apply PCA
    pca = PCA(n_components=0.95)  # Keep 95% of variance
    df_pca = pca.fit_transform(df_scaled)

    # Convert to a new DataFrame
    df_pca = pd.DataFrame(df_pca, columns=[f'PC{i+1}' for i in range(df_pca.shape[1])])

    # Check the shape of the new DataFrame
    print(df_pca.shape)

    # Run VIF on the PCA-transformed data
    # You would need to use the same VIF function as before on `df_pca`

    # If you want to inverse transform to get approximations of the original data
    df_approx = pca.inverse_transform(df_pca)
    df_approx = scaler.inverse_transform(df_approx)
    df_approx = pd.DataFrame(df_approx, columns=combined_df.columns)

    pd.set_option('display.max_columns', None)
    print(df_approx)


calculate_pairwise_correlation()
calculate_pairwise_distribution()
calculate_PCA_for_metrics()

