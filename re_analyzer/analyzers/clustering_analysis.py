from sklearn.manifold import TSNE
from umap import UMAP
from sklearn.cluster import KMeans
from sklearn.cluster import DBSCAN
from hdbscan import HDBSCAN

from re_analyzer.analyzers.clustering_analysis_util import visualize_cluster_density_heatmaps, visualize_clusters_by_input_features, visualize_clusters_by_input_features, visualize_clusters_by_adjusted_clustering_labels, calculate_cohesion_metrics, visualize_hierarchical_clusters, visualize_hiearchical_clusters_by_clustering_labels
from re_analyzer.analyzers.preprocessing import load_data, preprocess_dataframe, FilterMethod


# This method ingest the features in case we wish to incorporate either selective or all features for analysis.
def apply_dimensionality_reduction_and_clustering(df):
    # Dimensionality Reduction
    tsne = TSNE(n_components=2, random_state=42).fit_transform(df)
    umap = UMAP(n_components=2, random_state=42).fit_transform(df)

    # Clustering
    kmeans = KMeans(n_clusters=5, random_state=42)
    tsne_dbscan = DBSCAN(eps=5, min_samples=15)
    umap_dbscan = DBSCAN(eps=0.5, min_samples=15)
    hdbscan = HDBSCAN(min_samples=10, min_cluster_size=15)

    # Clustering on Dimensionality Reduction Results
    kmeans_tsne, kmeans_umap = kmeans.fit_predict(tsne), kmeans.fit_predict(umap)
    dbscan_tsne, dbscan_umap = tsne_dbscan.fit_predict(tsne), umap_dbscan.fit_predict(umap)
    hdbscan_tsne, hdbscan_umap = hdbscan.fit_predict(tsne), hdbscan.fit_predict(umap)

    clustering_labels_tsne = {
        'kMeans': kmeans_tsne,
        'DBSCAN': dbscan_tsne,
        'HDBSCAN': hdbscan_tsne
    }
    
    clustering_labels_umap = {
        'kMeans': kmeans_umap,
        'DBSCAN': dbscan_umap,
        'HDBSCAN': hdbscan_umap
    }
    
    return tsne, umap, clustering_labels_tsne, clustering_labels_umap

def main():
    combined_df = load_data()

    df_preprocessed, preprocessor = preprocess_dataframe(combined_df, filter_method=FilterMethod.FILTER_P_SCORE)
    tsne, umap, clustering_labels_tsne, clustering_labels_umap = apply_dimensionality_reduction_and_clustering(df_preprocessed)

    visualize_cluster_density_heatmaps(tsne, umap)
    visualize_clusters_by_input_features(tsne, umap, preprocessor.inverse_transform(df_preprocessed)[preprocessor.num_cols])
    visualize_clusters_by_adjusted_clustering_labels(tsne, umap, clustering_labels_tsne, clustering_labels_umap)
    print("t-SNE metrics", calculate_cohesion_metrics(tsne, clustering_labels_tsne))
    print("UMAP metrics", calculate_cohesion_metrics(umap, clustering_labels_umap))
    visualize_hierarchical_clusters(df_preprocessed)
    visualize_hiearchical_clusters_by_clustering_labels(tsne, umap, number_clusters=10)

if __name__ == '__main__':
    main()
