import os
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

from re_analyzer.utility.utility import PROPERTY_DATA_PATH


PROPERTY_DF_PATH = os.path.join(PROPERTY_DATA_PATH, 'property_static_df.parquet')

def load_data(drop_strings = True):
    property_df = pd.read_parquet(PROPERTY_DF_PATH)
    if drop_strings:
        property_df.drop(['street_address', 'image_url', 'property_url', 'city', 'is_waterfront'], axis=1, inplace=True)
        # property_df.drop(columns=property_df.select_dtypes(include=['object']).columns, inplace=True)
    return property_df

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

    def set_numeric_columns(self, numeric_columns):
        self.num_cols = numeric_columns
        self.num_cols_set = set(numeric_columns)

    def drop_columns(self, X, columns_to_drop):
        X = X.drop(columns=columns_to_drop)
        self.num_cols = [col for col in self.num_cols if col not in columns_to_drop]
        self.num_cols_set = set(self.num_cols)

        num_preprocessor = Pipeline(steps=[
            ('inputer', SimpleImputer(strategy='mean')),
            ('scaler', StandardScaler())
        ])
        transformers = [('num', num_preprocessor, self.num_cols)]
        if self.cat_cols:
            cat_preprocessor = Pipeline(steps=[
                ('inputer', SimpleImputer(strategy='constant')),
                ('ohe', OneHotEncoder())
            ])
            transformers.append(('cat', cat_preprocessor, self.cat_cols))
        self.transformers = transformers

        return X

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
        super().fit(X, y)
        self._post_fit_processing()
        return self

    def _post_fit_processing(self):
        if hasattr(self, 'cat_cols') and self.cat_cols:
            ohe_transformer = self.named_transformers_['cat'].named_steps['ohe']
            ohe_columns = ohe_transformer.get_feature_names_out(self.cat_cols)
            for ohe_col in ohe_columns:
                original_cat, cat_value = ohe_col.split('_', 1)
                self.cat_to_ohe_cols[original_cat].add(ohe_col)
                self.ohe_to_cat_value[ohe_col] = cat_value

    def df_transform(self, X):
        fitted_data = super().transform(X)

        ohe_cols = list(self.named_transformers_['cat'].get_feature_names_out()) if hasattr(self, 'cat_cols') and self.cat_cols else []

        cat_to_ohe_cols = defaultdict(set)
        for ohe_col in ohe_cols:
            cat_to_ohe_cols[ohe_col.split('_')[0]].add(ohe_col)

        df_preprocess = pd.DataFrame(fitted_data, columns=self.num_cols + ohe_cols, index=X.index)
        self.df_inverted = df_preprocess

        return df_preprocess

    def inverse_transform(self, df):
        inverted_df = pd.DataFrame(index=df.index)

        fitted_num_preprocessor = self.named_transformers_['num']
        scaler = fitted_num_preprocessor.named_steps['scaler']

        missing_cols = set(self.df_inverted.columns) - set(df.columns)
        df[list(missing_cols)] = self.df_inverted[list(missing_cols)]

        num_data = scaler.inverse_transform(df[self.num_cols])
        for i, col in enumerate(self.num_cols):
            inverted_df[col] = num_data[:, i]

        if hasattr(self, 'cat_cols') and self.cat_cols:
            for cat, ohe_set in self.cat_to_ohe_cols.items():
                cat_ohe_df = df[list(ohe_set)]
                inverted_df[cat] = cat_ohe_df.idxmax(axis=1).apply(lambda x: self.ohe_to_cat_value[x])

        inverted_df = inverted_df.drop(missing_cols & self.num_cols_set, axis=1)

        return inverted_df

def preprocess_dataframe(df, filter_method=FilterMethod.FILTER_NONE, cols_to_keep=set()):
    num_cols = set(df.select_dtypes(include=['int64', 'float32', 'float64']).columns)
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

    transformers = [('num', num_preprocessor, num_cols)]
    if cat_cols:
        transformers.append(('cat', cat_preprocessor, cat_cols))

    preprocessor = InvertibleColumnTransformer(transformers=transformers)
    
    features_to_remove = [col for col in features_to_remove_by_vif(df_preprocess[num_cols]) if col not in cols_to_keep]
    print(features_to_remove, cols_to_keep)
    df_preprocess = preprocessor.drop_columns(df_preprocess, features_to_remove)
    
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
