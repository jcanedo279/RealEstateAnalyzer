from sklearn.manifold import TSNE
from umap import UMAP
from sklearn.cluster import KMeans
from sklearn.cluster import DBSCAN
from hdbscan import HDBSCAN
from sklearn.ensemble import IsolationForest

from zillowanalyzer.analyzers.clustering_analysis_util import *
from zillowanalyzer.analyzers.preprocessing import load_data, preprocess_data


# This method ingest the features in case we wish to incorporate either selective or all features for analysis.
def apply_dimensionality_reduction_and_clustering(df_scaled):
    # Dimensionality Reduction
    tsne = TSNE(n_components=2, random_state=42).fit_transform(df_scaled)
    umap = UMAP(n_components=2, random_state=42).fit_transform(df_scaled)
    
    # Clustering
    kmeans = KMeans(n_clusters=5, random_state=42)
    tsne_dbscan = DBSCAN(eps=5, min_samples=7)
    umap_dbscan = DBSCAN(eps=0.2, min_samples=7)
    hdbscan = HDBSCAN(min_samples=10, min_cluster_size=15)
    
    # Isolation Forest for Outlier Detection
    iso_forest = IsolationForest(n_estimators=100, contamination='auto', random_state=42).fit(df_scaled)
    outliers = iso_forest.predict(df_scaled)  # Predict outliers using the original dataset
    
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
    
    return tsne, umap, outliers, clustering_labels_tsne, clustering_labels_umap

def main():
    target_features = ['Home Price Beta', 'Home Price Alpha', 'purchase_price', 'gross_rent_multiplier', 'adj_CoC 5.0% Down']

    all_columns, combined_df = load_data()
    df_scaled, filtered_df = preprocess_data(combined_df, target_features)
    tsne, umap, outliers, clustering_labels_tsne, clustering_labels_umap = apply_dimensionality_reduction_and_clustering(df_scaled)

    visualize_cluster_density_heatmaps(tsne, umap)
    visualize_isolation_forest_outliers(tsne, umap, outliers)
    visualize_clusters_by_input_features(tsne, umap, df_scaled)
    visualize_clusters_by_adjusted_clustering_labels(tsne, umap, clustering_labels_tsne, clustering_labels_umap)
    print("t-SNE metrics", calculate_cohesion_metrics(tsne, clustering_labels_tsne))
    print("UMAP metrics", calculate_cohesion_metrics(umap, clustering_labels_umap))
    visualize_hierarchical_clusters(df_scaled)
    visualize_hiearchical_clusters_by_clustering_labels(tsne, umap, number_clusters=10)

if __name__ == '__main__':
    main()
