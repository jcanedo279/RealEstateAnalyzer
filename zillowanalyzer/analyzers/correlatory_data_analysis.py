import os
import numpy as np
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
from sklearn.decomposition import PCA
from statsmodels.stats.outliers_influence import variance_inflation_factor

from zillowanalyzer.analyzers.preprocessing import load_data, preprocess_data
from zillowanalyzer.scrapers.scraping_utility import VISUAL_DATA_PATH, ensure_directory_exists


CORRELATORY_VISUAL_DATA_PATH = os.path.join(VISUAL_DATA_PATH, 'correlatory')
ensure_directory_exists(CORRELATORY_VISUAL_DATA_PATH)

#########################
## NON_VISUAL ANALYSIS ##
#########################

def calculate_vif(dataframe):
    """
    Calculates VIF for each numeric feature in the DataFrame.
    """
    vif_data = pd.DataFrame()
    vif_data["Feature"] = dataframe.columns
    vif_data["VIF"] = [variance_inflation_factor(dataframe.values, i) for i in range(dataframe.shape[1])]
    return vif_data

def calculate_multicollinearity(filtered_df, features):
    """
    Wrapper function to calculate and print VIF for specified features.
    """
    numeric_df = filtered_df[features].select_dtypes(include=[np.number])
    vif_dataframe = calculate_vif(numeric_df)
    print("VIF Data:\n", vif_dataframe)

def calculate_pca(df_scaled, features, scaler):
    """
    Applies PCA on scaled data to keep 95% of variance and optionally calculates VIF for components.
    """
    pca = PCA(n_components=0.95)
    pca_transformed = pca.fit_transform(df_scaled)

    # Creating a DataFrame for the PCA components
    pca_df = pd.DataFrame(pca_transformed, columns=[f'PC{i+1}' for i in range(pca_transformed.shape[1])])
    print("PCA Components Shape:", pca_df.shape)

    # Optionally calculate VIF for PCA components
    vif_dataframe = calculate_vif(pca_df)
    print("VIF for PCA Components:\n", vif_dataframe)

    # Inverse transform to get approximations of the original data
    approx_original_data = scaler.inverse_transform(pca.inverse_transform(pca_transformed))
    approx_df = pd.DataFrame(approx_original_data, columns=features)
    print("Approximated Original Data (First Few Rows):\n", approx_df.head())

#####################
## VISUAL ANALYSIS ##
#####################

def visualize_pairwise_correlation(df_scaled):
    corr_matrix = df_scaled.corr()
    plt.figure(figsize=(10, 8))
    sns.heatmap(corr_matrix, annot=True, cmap='coolwarm', cbar=True, fmt=".2f")
    plt.title("Pairwise Correlation")
    plt.tight_layout()
    plt.savefig(f'{CORRELATORY_VISUAL_DATA_PATH}/pairwise_correlation.png', dpi=300)

def visualize_pairwise_distribution(df_scaled):
    sns.pairplot(df_scaled, diag_kind='kde')
    plt.savefig(f'{CORRELATORY_VISUAL_DATA_PATH}/pairwise_distribution.png')



def main():
    target_features = ['Home Price Beta', 'Home Price Alpha', 'purchase_price', 'gross_rent_multiplier', 'adj_CoC 5.0% Down']

    all_columns, combined_df = load_data()
    df_scaled, filtered_df, scaler = preprocess_data(combined_df, target_features, return_scaler=True)
    
    calculate_vif(df_scaled)
    calculate_multicollinearity(filtered_df, target_features)
    calculate_pca(df_scaled, target_features, scaler)

    visualize_pairwise_correlation(df_scaled)
    visualize_pairwise_distribution(df_scaled)

if __name__ == '__main__':
    main()
