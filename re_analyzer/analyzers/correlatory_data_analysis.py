import os
import numpy as np
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
from sklearn.decomposition import PCA

from re_analyzer.analyzers.preprocessing import load_data, preprocess_dataframe, FilterMethod, calculate_vif
from re_analyzer.utility.utility import VISUAL_DATA_PATH, ensure_directory_exists


CORRELATORY_VISUAL_DATA_PATH = os.path.join(VISUAL_DATA_PATH, 'correlatory')
ensure_directory_exists(CORRELATORY_VISUAL_DATA_PATH)

#########################
## NON_VISUAL ANALYSIS ##
#########################

def calculate_multicollinearity(filtered_df, features, max_VIF=5):
    """
    Wrapper function to calculate VIF for specified features and filter out features with VIF >= 5.
    """
    numeric_df = filtered_df[features].select_dtypes(include=[np.number])
    vif_dataframe = calculate_vif(numeric_df)
    print("VIF Data:\n", vif_dataframe)

    filtered_features = vif_dataframe[vif_dataframe["VIF"] < max_VIF]["Feature"].tolist()
    return filtered_features

def calculate_pca(df_scaled, features):
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


#####################
## VISUAL ANALYSIS ##
#####################

def visualize_pairwise_correlation(df_scaled, path=f'{CORRELATORY_VISUAL_DATA_PATH}/pairwise_correlation.png', title="Pairwise Correlation"):
    corr_matrix = df_scaled.corr()
    plt.figure(figsize=(10, 8))
    sns.heatmap(corr_matrix, cmap='coolwarm', cbar=True)
    plt.title(title)
    plt.tight_layout()
    plt.savefig(path, dpi=300)
    plt.close()

def visualize_pairwise_distribution(df_scaled, path=f'{CORRELATORY_VISUAL_DATA_PATH}/pairwise_distribution.png', title="Pairwise Distribution"):
    plt.figure(figsize=(10, 8))
    pairplot = sns.pairplot(df_scaled, diag_kind='kde', corner=True)
    pairplot.fig.suptitle(title)
    plt.tight_layout()
    plt.savefig(path, dpi=300)
    plt.close()



def main():
    combined_df = load_data()
    df_preprocessed, preprocessor = preprocess_dataframe(combined_df, filter_method=FilterMethod.FILTER_P_SCORE)
    target_features = df_preprocessed.columns
    
    filtered_features = calculate_multicollinearity(df_preprocessed, target_features)

    # calculate_pca(df_preprocessed, target_features)

    df_inverse_preprocess = preprocessor.inverse_transform(df_preprocessed)[preprocessor.num_cols]
    visualize_pairwise_correlation(df_inverse_preprocess)
    visualize_pairwise_distribution(df_inverse_preprocess)

if __name__ == '__main__':
    main()
