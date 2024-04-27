import os
import numpy as np
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
from sklearn.metrics import silhouette_score, calinski_harabasz_score, davies_bouldin_score
from scipy.cluster.hierarchy import dendrogram, linkage, fcluster

from re_analyzer.utility.utility import VISUAL_DATA_PATH, ensure_directory_exists


CLUSTER_VISUAL_DATA_PATH = os.path.join(VISUAL_DATA_PATH, 'cluster')
ensure_directory_exists(CLUSTER_VISUAL_DATA_PATH)


#####################
## GENERAL UTILITY ##
#####################
    
def format_label(value):
    if abs(value) >= 1000 or abs(value) < 0.01 and value != 0:
        # Use scientific notation for large numbers or very small numbers
        return f"{value:.2e}"
    else:
        # Use regular formatting for numbers that do not require scientific notation
        return f"{value:.2f}"

def adjust_labels_to_bins(labels, n_bins=8):
    """
    Method to adjust clustering labels into a fixed number (n_bins) of bins.
    """
    # Identify noise points
    is_noise = labels == -1
    
    # For non-noise labels, rescale to range [0, n_bins-1]
    min_label = labels[~is_noise].min()
    max_label = labels[~is_noise].max()
    scale = (n_bins - 1) / (max_label - min_label)
    adjusted_labels = np.where(is_noise, -1, np.floor((labels - min_label) * scale).astype(int))

    return adjusted_labels


######################
## GRAPHING UTILITY ##
######################

def plot_scatter(data, labels, title, ax, x_label='', y_label='', legend_title='', is_outlier=False):
    if is_outlier:
        ax.scatter(data[:, 0], data[:, 1], color=['blue' if x == 1 else 'red' for x in labels], edgecolor='k', alpha=0.7)
    else:
        sns.scatterplot(x=data[:, 0], y=data[:, 1], hue=labels, palette='rocket', legend='full', ax=ax)
    ax.set_title(title)
    if x_label:
        ax.set_xlabel(x_label)
    if y_label:
        ax.set_ylabel(y_label)
    if legend_title:
        ax.legend(title=legend_title, bbox_to_anchor=(1.05, 1), loc='best')
    ax.invert_yaxis()  # For consistency with t-SNE/UMAP plots

def generate_and_save_dendrogram(linkage_matrix, title, save_path, number_clusters=5):
    """
    Generates and saves a dendrogram plot from a given linkage matrix.
    """
    color_threshold = max(linkage_matrix[:, 2]) / 2
    fig, ax = plt.subplots(figsize=(15, 5))
    dendrogram(linkage_matrix, ax=ax, truncate_mode='lastp', p=number_clusters, show_leaf_counts=True, color_threshold=color_threshold)
    plt.title(title)
    plt.xlabel('Sample Index or (Cluster Size)')
    plt.ylabel('Distance')
    plt.tight_layout()
    plt.savefig(save_path)
    plt.close(fig)


#############################
## NON-VISUAL CALCULATIONS ##
#############################

def calculate_cohesion_metrics(X, method_to_labels):
    """
    Calculate metrics relating to clustering cohesion/performance.
    """
    method_to_cohesion_metrics = {}
    for method, labels in method_to_labels.items():
        # Calculate silhouette score only if there are 2 or more clusters, otherwise it is not defined
        silhouette_avg = silhouette_score(X, labels) if len(set(labels)) > 1 else -1
        # Calculate Calinski-Harabasz score only if there are 2 or more clusters
        calinski_harabasz = calinski_harabasz_score(X, labels) if len(set(labels)) > 1 else -1
        # Calculate Davies-Bouldin score only if there are 2 or more clusters
        davies_bouldin = davies_bouldin_score(X, labels) if len(set(labels)) > 1 else -1
        method_to_cohesion_metrics[method] = {"silhouette": silhouette_avg, "calinski_harabasz": calinski_harabasz, "davies_bouldin": davies_bouldin}
    return method_to_cohesion_metrics


################################
## CLUSTERING VISUAL ANALYSIS ##
################################
    
def visualize_hierarchical_clusters(df_scaled, method='ward'):
    """
    Wrapper function to perform hierarchical clustering and plot dendrogram.
    """
    linkage_matrix = linkage(df_scaled, method=method)
    desired_clusters = 5  # Example: aiming for 5 clusters
    cluster_labels = fcluster(linkage_matrix, t=desired_clusters, criterion='maxclust')
    generate_and_save_dendrogram(linkage_matrix, 'Dendrogram for dataset', os.path.join(CLUSTER_VISUAL_DATA_PATH, "hierarchical_cluster.png"))
    return cluster_labels

def visualize_hiearchical_clusters_by_clustering_labels(tsne_results, umap_results, number_clusters=5, method='ward'):
    # Calculate linkage matrices
    linkage_tsne = linkage(tsne_results, method=method)
    linkage_umap = linkage(umap_results, method=method)

    # Generate and save dendrograms
    generate_and_save_dendrogram(linkage_tsne, 'Dendrogram for t-SNE Results', os.path.join(CLUSTER_VISUAL_DATA_PATH, "dendrogram_tsne.png"), number_clusters=number_clusters)
    generate_and_save_dendrogram(linkage_umap, 'Dendrogram for UMAP Results', os.path.join(CLUSTER_VISUAL_DATA_PATH, "dendrogram_umap.png"), number_clusters=number_clusters)

    # Generate cluster labels
    hierarchical_labels_tsne = fcluster(linkage_tsne, t=number_clusters, criterion='maxclust')
    hierarchical_labels_umap = fcluster(linkage_umap, t=number_clusters, criterion='maxclust')

    # Visualize clusters by hierarchical labels
    fig, axes = plt.subplots(1, 2, figsize=(15, 7.5))
    plot_scatter(tsne_results, hierarchical_labels_tsne, "t-SNE with Hierarchical Clustering Labels", axes[0], is_outlier=False)
    plot_scatter(umap_results, hierarchical_labels_umap, "UMAP with Hierarchical Clustering Labels", axes[1], is_outlier=False)

    plt.tight_layout()
    plt.savefig(os.path.join(CLUSTER_VISUAL_DATA_PATH, "2D_clusters_by_hierarchical_labels.png"))

def visualize_cluster_density_heatmaps(tsne_results, umap_results):
    """
    Generates heatmaps for t-SNE and UMAP cluster density.
    """
    # Configuration for heatmaps
    num_bins = 50  # Defines the resolution of the heatmap
    heatmap_configs = [('t-SNE', tsne_results), ('UMAP', umap_results)]
    
    # Create subplots
    fig, axes = plt.subplots(1, 2, figsize=(16, 6))
    
    for ax, (title, data) in zip(axes, heatmap_configs):
        # Calculate the 2D histogram
        hist, x_edges, y_edges = np.histogram2d(data[:, 0], data[:, 1], bins=num_bins)
        
        # Plot the heatmap
        sns.heatmap(hist.T, cmap='rocket', cbar=True, ax=ax)
        ax.set_title(f'{title} Heatmap')
        ax.set_xlabel(f'{title} dimension 1')
        ax.set_ylabel(f'{title} dimension 2')

    plt.tight_layout()
    plt.savefig(os.path.join(CLUSTER_VISUAL_DATA_PATH, "2D_clusters_density_heatmaps.png"))

def visualize_clusters_by_input_features(tsne_results, umap_results, df):
    num_subplots = len(df.columns)
    fig, axes = plt.subplots(num_subplots, 2, figsize=(15, 5 * num_subplots))

    for i, feature in enumerate(df.columns):
        # Discretize feature into bins based on quantiles
        quantiles = np.linspace(0, 1, 6)
        bin_edges = np.quantile(df[feature], quantiles)
        # Ensure bin edges are unique by using np.unique
        unique_bin_edges = np.unique(bin_edges)
        
        # If binning results in less than 2 edges (not enough for binning), skip this feature
        if len(unique_bin_edges) < 2:
            print(f"Not enough unique bin edges for feature {feature}. Skipping.")
            continue

        # Sort labels and tsne/umap results for plotting
        bin_labels = pd.cut(df[feature], bins=unique_bin_edges, labels=False, include_lowest=True)
        sort_indices = np.argsort(bin_labels.to_numpy())
        sorted_bin_labels = bin_labels.iloc[sort_indices]
        sorted_tsne_results = tsne_results[sort_indices]
        sorted_umap_results = umap_results[sort_indices]

        # Generate formatted labels for the legend
        label_mapping = {i: f"{format_label(unique_bin_edges[i])} to {format_label(unique_bin_edges[i+1])}" for i in range(len(unique_bin_edges)-1)}

        bin_labels_formatted = sorted_bin_labels.map(label_mapping)

        # t-SNE
        plot_scatter(sorted_tsne_results, bin_labels_formatted, f't-SNE colored by {feature}', axes[i, 0])
        # UMAP
        plot_scatter(sorted_umap_results, bin_labels_formatted, f'UMAP colored by {feature}', axes[i, 1])

    plt.tight_layout()
    plt.savefig(os.path.join(CLUSTER_VISUAL_DATA_PATH, "2D_clusters_by_input_features.png"))

def visualize_clusters_by_adjusted_clustering_labels(tsne_results, umap_results, clustering_labels_tsne, clustering_labels_umap):
    methods = list(clustering_labels_tsne.keys())
    num_methods = len(methods)
    fig, axes = plt.subplots(num_methods, 2, figsize=(15, 5 * num_methods))

    for i, method in enumerate(methods):
        labels_tsne = adjust_labels_to_bins(clustering_labels_tsne[method], n_bins=8)
        labels_umap = adjust_labels_to_bins(clustering_labels_umap[method], n_bins=8)

        # t-SNE
        plot_scatter(tsne_results, labels_tsne, f't-SNE with adjusted {method} labels', axes[i, 0])
        # UMAP
        plot_scatter(umap_results, labels_umap, f'UMAP with adjusted {method} labels', axes[i, 1])

    plt.tight_layout()
    plt.savefig(os.path.join(CLUSTER_VISUAL_DATA_PATH, "2D_clusters_by_adjusted_clustering_labels.png"))
