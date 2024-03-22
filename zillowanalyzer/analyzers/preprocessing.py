import pandas as pd
import numpy as np
from enum import Enum, auto
from collections import defaultdict
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.ensemble import IsolationForest
from scipy import stats
from statsmodels.stats.outliers_influence import variance_inflation_factor

from zillowanalyzer.utility.utility import ALPHA_BETA_DATA_PATH, REAL_ESTATE_METRICS_DATA_PATH, HOME_FEATURES_DATAFRAME_PATH


def load_data():
    alpha_beta_df = pd.read_csv(ALPHA_BETA_DATA_PATH)
    property_metrics_df = pd.read_csv(REAL_ESTATE_METRICS_DATA_PATH).drop(['zip_code', 'street_address'], axis=1)
    combined_df = pd.merge(alpha_beta_df, property_metrics_df, on='zpid', how='inner').set_index('zpid')

    # Load from home features from disk.
    shap_output_df = pd.read_parquet(HOME_FEATURES_DATAFRAME_PATH)
    combined_df['home_features_score'] = shap_output_df['home_features_score']
    combined_df['is_waterfront'] = 1 - shap_output_df['waterView_None']
    return combined_df

def calculate_vif(dataframe):
    """
    Calculates VIF for each numeric feature in the DataFrame.
    """
    vif_data = pd.DataFrame()
    vif_data["Feature"] = dataframe.columns
    vif_data["VIF"] = [variance_inflation_factor(dataframe.values, i) for i in range(dataframe.shape[1])]
    return vif_data

def features_to_remove_by_vif(df, max_VIF=5):
    """
    Iteratively removes features until all remaining features have a VIF less than the specified threshold.
    """
    features = list(df.columns)
    features_to_remove = []
    while True:
        # Calculate VIF
        df_vif = calculate_vif(df[features])
        # Check if there's any feature above the threshold
        if df_vif['VIF'].max() <= max_VIF:
            break
        
        # Identify the feature with the highest VIF
        feature_to_remove = df_vif.sort_values('VIF', ascending=False).iloc[0]['Feature']
        print(f"Removing feature: {feature_to_remove} with VIF: {df_vif['VIF'].max()}")
        
        # Remove the feature with the highest VIF
        features.remove(feature_to_remove)
        features_to_remove.append(feature_to_remove)
    
    # Return the filtered features
    return features_to_remove

class FilterMethod(Enum):
    FILTER_NONE = auto()
    FILTER_IQR = auto()
    FILTER_P_SCORE = auto()
    FILTER_ISO_FOREST = auto()

class InvertibleColumnTransformer(ColumnTransformer):
    def __init__(self, transformers, remainder='drop', sparse_threshold=0.3, n_jobs=None, transformer_weights=None, verbose=False):
        super().__init__(transformers, remainder=remainder, sparse_threshold=sparse_threshold, n_jobs=n_jobs, transformer_weights=transformer_weights, verbose=verbose)
        self.df_inverted = None
        self.cat_to_ohe_cols = defaultdict(set)
        self.ohe_to_cat_value = {}
        
        for name, transformer, columns in self.transformers:
            if name == 'num':
                self.num_cols = columns
                self.num_cols_set = set(columns)
            elif name == 'cat':
                self.cat_cols = columns

    def filter_dataframe(self, X, filter_method):
        if filter_method == FilterMethod.FILTER_NONE:
            self.outliers = pd.DataFrame(columns=X.columns)
            return X
        df_filtered = None
        outliers = None
        if filter_method == FilterMethod.FILTER_IQR:
            Q1, Q3 = X[self.num_cols].quantile(0.25), X[self.num_cols].quantile(0.75)
            IQR = Q3 - Q1
            mask = ((X[self.num_cols] >= (Q1 - 1.5 * IQR)) & (X[self.num_cols] <= (Q3 + 1.5 * IQR))).all(axis=1)
            df_filtered = X[mask]
            outliers = X[~mask]
        elif filter_method == FilterMethod.FILTER_P_SCORE:
            z_scores = abs(stats.zscore(X[self.num_cols]))
            mask = (z_scores < 3).all(axis=1)
            df_filtered = X[mask]
            outliers = X[~mask]
        elif filter_method == FilterMethod.FILTER_ISO_FOREST:
            iso_forest = IsolationForest(n_estimators=100, contamination='auto', random_state=42).fit(X[self.num_cols])
            preds = iso_forest.predict(X)
            mask = preds == 1
            df_filtered = X[mask]
            outliers = X[~mask]
        self.outliers = outliers
        return df_filtered

    def fit(self, X, y=None):
        # Call the fit method of the base class
        super().fit(X, y)
        self._post_fit_processing()
        return self
        
    def _post_fit_processing(self):
        # Assuming cat_preprocessor is the name of the pipeline step for categorical preprocessing
        # and it's the second transformer in your setup
        ohe_transformer = self.named_transformers_['cat'].named_steps['ohe']
        ohe_columns = ohe_transformer.get_feature_names_out(self.cat_cols)
        for ohe_col in ohe_columns:
            # Splitting the one-hot encoded column name to extract the category and its value
            original_cat, cat_value = ohe_col.split('_', 1)
            self.cat_to_ohe_cols[original_cat].add(ohe_col)
            self.ohe_to_cat_value[ohe_col] = cat_value

    def df_transform(self, X):
        fitted_data = super().transform(X)

        ohe_cols = list( self.named_transformers_['cat'].get_feature_names_out() )

        cat_to_ohe_cols = defaultdict(set)
        for ohe_col in ohe_cols:
            cat_to_ohe_cols[ohe_col.split('_')[0]].add(ohe_col)

        df_preprocess = pd.DataFrame(fitted_data, columns=self.num_cols+ohe_cols, index=X.index)
        self.df_inverted = df_preprocess

        return df_preprocess

    def inverse_transform(self, df):
        # Initialize an empty dataframe to hold the inverted data
        inverted_df = pd.DataFrame(index=df.index)

        # Access the fitted numerical preprocessor pipeline
        fitted_num_preprocessor = self.named_transformers_['num']
        scaler = fitted_num_preprocessor.named_steps['scaler']

        # Fill in missing columns.
        missing_cols = set(self.df_inverted.columns) - set(df.columns)
        df[list(missing_cols)] = self.df_inverted[list(missing_cols)]

        # Inverse transform for numerical data
        num_data = scaler.inverse_transform(df[self.num_cols])
        for i, col in enumerate(self.num_cols):
            inverted_df[col] = num_data[:, i]

        # Inverse transform for categorical data
        for cat, ohe_set in self.cat_to_ohe_cols.items():
            cat_ohe_df = df[list(ohe_set)]
            # Use the stored mapping to convert back to original categories
            inverted_df[cat] = cat_ohe_df.idxmax(axis=1).apply(lambda x: self.ohe_to_cat_value[x])
        
        # Drop missing columns which are also numeric.
        inverted_df = inverted_df.drop(missing_cols & self.num_cols_set, axis=1)

        return inverted_df


def preprocess_dataframe(df, filter_method = FilterMethod.FILTER_NONE):
    num_cols = set( df.select_dtypes(include=['int64', 'float32', 'float64']).columns )
    cat_cols = [column for column in df.columns if column not in num_cols]
    num_cols = list(num_cols)

    df = df.replace([np.inf, -np.inf], np.nan).dropna()

    df_preprocess = df
    df_preprocess[cat_cols] = df_preprocess[cat_cols].astype('category')

    num_preprocessor = Pipeline(steps=[
        ('inputer', SimpleImputer(strategy='mean')),
        ('scaler', StandardScaler())
    ])
    cat_preprocessor = Pipeline(steps=[
        ('inputer', SimpleImputer(strategy='constant')),
        ('ohe', OneHotEncoder())
    ])
    preprocessor = InvertibleColumnTransformer(
        transformers=[
            ('num', num_preprocessor, num_cols),
            ('cat', cat_preprocessor, cat_cols)
        ]
    )
    # Remove columns (features) with high multicollinearity (VIF) with other features.
    features_to_remove = features_to_remove_by_vif(df_preprocess[num_cols])
    df_preprocess.drop(columns=features_to_remove)
    # Remove rows (instances) which are deemd "outliers".
    df_preprocess = preprocessor.filter_dataframe(df_preprocess, filter_method=filter_method)
    preprocessor.fit(df_preprocess)
    df_preprocess = preprocessor.df_transform(df_preprocess)
    
    return df_preprocess, preprocessor


def preprocess_test():
    import random as rd

    num_samps = 100
    cat_1_vals = ['a', 'b', 'c']
    cat_2_vals = ['aa', 'bb', 'cc']

    data_mock = [{"num1": 10*rd.random(), "num2": 3*rd.random(), "cat1": rd.choice(cat_1_vals), "cat2": rd.choice(cat_2_vals)} for _ in range(num_samps)]
    df_mock = pd.DataFrame.from_records(data_mock, columns=data_mock[0].keys())
    print(df_mock)

    df_preprocess, preprocessor = preprocess_dataframe(df_mock)
    print(df_preprocess)

    df_preprocess_dropped = df_preprocess.drop(['num1', 'cat1_a'], axis=1)

    df_mock_dropped = preprocessor.inverse_transform(df_preprocess_dropped)
    print(df_mock_dropped)


if __name__ == '__main__':
    preprocess_test()
