from sklearn.model_selection import train_test_split
from xgboost import XGBRegressor
from sklearn.metrics import r2_score
import matplotlib.pyplot as plt
import json
import requests
import pandas as pd
import sys

from zillowanalyzer.analyzers.preprocessing import load_data, preprocess_dataframe, FilterMethod
from zillowanalyzer.analyzers.iterator import get_property_info_from_property_details
from zillowanalyzer.utility.utility import VISUAL_DATA_PATH, DATA_PATH, PROPERTY_DETAILS_PATH

combined_df = load_data()
# target_zip_codes = {33859,33863,33831,33813,33846,33884,33812,33880,33839,33811,33877,33807,33885,33566,33838,33840,33803,33563,33564,33882,33883,33888,33804,33802,33815,33801,33806,33851,33881,33844,33823,33850,33805,33565,33845,33810,33809,33868}
# combined_df = combined_df[combined_df['zip_code'].isin(target_zip_codes)]
print(combined_df.shape[0])

cols_to_ignore = ['annual_homeowners_insurance']
combined_df.drop(cols_to_ignore, axis=1, inplace=True)
df_preprocessed, preprocessor = preprocess_dataframe(combined_df, filter_method=FilterMethod.FILTER_P_SCORE)


predictor = 'purchase_price'
X = df_preprocessed.drop(columns=[predictor], errors='ignore')
y = df_preprocessed[predictor]

model = XGBRegressor(objective='reg:squarederror')

def calc_unexplained_price_variance(drop_zip_code = False):
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)
    if drop_zip_code:
        X_train.drop('zip_code', axis=1, inplace=True)
        X_test.drop('zip_code', axis=1, inplace=True)

    # Initialize and train your model
    model.fit(X_train, y_train)

    # Predict on the test set
    y_pred = model.predict(X_test)

    # Calculate R-squared
    r_squared = r2_score(y_test, y_pred)

    # Calculate the percentage of variance NOT explained by the model
    percentage_unexplained_variance = (1 - r_squared) * 100

    importances = pd.Series(model.feature_importances_, index=X_train.columns)
    return percentage_unexplained_variance, importances

def calc_multi_unexplained_price_variance(drop_zip_code = False, num_samples = 100):
    percentage_unexplained_variances = []
    cumulative_importances = pd.DataFrame(0, index=X.columns, columns=['Importance'])
    for _ in range(num_samples):
        percentage_unexplained_variance, importances = calc_unexplained_price_variance(drop_zip_code = drop_zip_code)
        cumulative_importances['Importance'] += importances
        percentage_unexplained_variances.append(percentage_unexplained_variance)
    cumulative_importances['Importance']
    cumulative_importances.sort_values(by='Importance', ascending=False, inplace=True)
    cumulative_importances['Cumulative Importance'] = cumulative_importances['Importance'] / cumulative_importances['Importance'].sum()
    return percentage_unexplained_variances, cumulative_importances

def calc_and_save_cum_unexplained_price_variance(drop_zip_code = False):
    percentage_unexplained_variances, cumulative_importances = calc_multi_unexplained_price_variance(drop_zip_code = drop_zip_code)
    percentage_unexplained_variance = sum(percentage_unexplained_variances) / len(percentage_unexplained_variances)
    zip_code_incl_string = 'without' if drop_zip_code else 'with'
    print(f"Percentage of variance not explained by the model {zip_code_incl_string} 'zip_code': {percentage_unexplained_variance:.2f}%")
    print(cumulative_importances)

    plt.hist(percentage_unexplained_variances, bins=20, alpha=0.75, color='blue', edgecolor='black')
    plt.title('Histogram of Unexplained Variances')
    plt.xlabel('Percentage of Unexplained Variance')
    plt.ylabel('Frequency')
    plt.savefig(f'{VISUAL_DATA_PATH}/distributional/unexplained_price_var_by_input_features_{zip_code_incl_string}_zip_code.png')
    plt.close()

calc_and_save_cum_unexplained_price_variance(drop_zip_code=True)
calc_and_save_cum_unexplained_price_variance()

df_preprocessed['predicted_price'] = model.predict(df_preprocessed.drop(['purchase_price'], axis=1))
df_preprocessed['residual'] = df_preprocessed['purchase_price'] - df_preprocessed['predicted_price']

threshold = df_preprocessed['residual'].quantile(0.05)
underpriced_homes = df_preprocessed[df_preprocessed['residual'] <= threshold]

sorted_underpriced_homes = df_preprocessed.sort_values(by='residual')

inv_sorted_underpriced_homes = preprocessor.inverse_transform(sorted_underpriced_homes)

inv_sorted_underpriced_homes.to_csv('proc.csv')

print('='*30)

max_rows = 20
underpriced_homes_iter = inv_sorted_underpriced_homes.head(max_rows).iterrows()
home_num = 0
for index, home in underpriced_homes_iter:
    zip_code, zpid = int(home['zip_code']), int(index)
    with open(f'{PROPERTY_DETAILS_PATH}/{zip_code}/{zpid}_property_details.json', 'r') as json_file:
        property_details = json.load(json_file)
        # Yield the loaded JSON data
        if 'props' not in property_details:
            continue
        property_info = get_property_info_from_property_details(property_details)
        image_url = property_info['originalPhotos'][0]['mixedSources']['jpeg'][1]['url']

        # Send a GET request to the image URL
        response = requests.get(image_url)

        # Check if the request was successful
        if response.status_code == 200:
            # Specify the local path where you want to save the image
            image_path = f"{DATA_PATH}/InvestmentSelections/im_{home_num}.jpg"
            
            # Open the specified path in binary write mode and save the image
            with open(image_path, "wb") as file:
                file.write(response.content)
        else:
            print(f"Failed to download image. Status code: {response.status_code}")
    home_num += 1
