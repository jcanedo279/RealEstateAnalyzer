import os
import json
import glob
import yfinance as yf
import pandas as pd
import warnings
warnings.simplefilter(action="ignore", category=FutureWarning)

import sys


def deannualize(annual_rate, periods=365):
    return (1 + annual_rate) ** (1/periods) - 1

def fetch_risk_free_rate(start_date):
    df_rf = yf.download("^IRX", start=start_date, progress=False)
    df_rf['Risk Free Rate'] = df_rf['Adj Close'].apply(lambda x: deannualize(x/100))
    return df_rf['Risk Free Rate']

# Function to fetch historical data for a given ticker
def fetch_stock_data_frame(ticker, starting_date):
    df = yf.download(ticker, start=starting_date, progress=False)
    # Ensure the datetime index is timezone-naive for compatibility.
    df.index = df.index.tz_localize(None)
    return df

def calculate_alpha_beta_for_property(json_file, index_ticker):
    zestimate_history_data = json.load(json_file)
    # Extract and convert time series from json to a Pandas DF.
    zestimate_history = zestimate_history_data['zestimateHistory']
    if len(zestimate_history) <= 3:
        return
    zestimate_history_df = pd.DataFrame(zestimate_history)
    # Convert dates from UTC in ms to datetime objects.
    # zestimate_history_df['x'] = pd.to_datetime(zestimate_history_df['x'], unit='ms')
    zestimate_history_df.set_index('Date', inplace=True)
    # Drop the specific date times and only keep the date itself.
    zestimate_history_df.index = pd.to_datetime(zestimate_history_df.index).normalize()
    zestimate_history_df = zestimate_history_df.sort_index(ascending=True)
    zestimate_history_df.rename(columns={'Price' : 'Home Price'}, inplace=True)

    index_df = fetch_stock_data_frame(index_ticker, zestimate_history_df.index[0])
    df_rf = fetch_risk_free_rate(zestimate_history_df.index[0])

    aligned_df = pd.merge_asof(zestimate_history_df, index_df, on='Date', direction='nearest')
    aligned_df = pd.merge_asof(aligned_df, df_rf, on='Date', direction='nearest')
    aligned_df.set_index('Date', inplace=True)

    aligned_df['Home Price Returns'] = aligned_df['Home Price'].pct_change()
    aligned_df['Stock Returns'] = aligned_df['Adj Close'].pct_change()
    # Get rid of the first row since it does not have a percent change reference.
    aligned_df.dropna(inplace=True)

    cov_matrix = aligned_df[['Home Price Returns', 'Stock Returns']].cov()
    beta = cov_matrix.loc['Home Price Returns', 'Stock Returns'] / aligned_df['Stock Returns'].var()
    
    # Calculate Alpha using a risk free rate and the CAPM formula.
    alpha = (aligned_df['Home Price Returns'] - aligned_df['Risk Free Rate']).mean() - beta * (aligned_df['Stock Returns'] - aligned_df['Risk Free Rate']).mean()
    
    return {"Alpha": alpha, "Beta": beta}



def calculate_alpha_beta_statistics(index_ticker):

    alpha_beta_values = []
    # Loop through each zip code.
    for zip_code_folder in glob.glob(os.path.join('PropertyDetails', '*')):
        zip_code = os.path.basename(zip_code_folder)
        
        # Process each JSON file within the zip code folder (each property).
        for json_file_path in glob.glob(os.path.join(zip_code_folder, '*_property_details.json')):
            with open(json_file_path, 'r') as json_file:
                try:
                    alpha_beta_value = calculate_alpha_beta_for_property(json_file, index_ticker)
                    if not alpha_beta_value:
                        continue
                    # We filter out excessively large values, we can debug why these exist later...
                    if abs(alpha_beta_value['Alpha']) > 1 or abs(alpha_beta_value['Beta']) > 1:
                        continue
                    alpha_beta_values.append(alpha_beta_value)
                except:
                    continue

    alpha_beta_df = pd.DataFrame(alpha_beta_values)
    # Calculate aggregate statistics.
    aggregate_statistics = alpha_beta_df.describe()
    print(aggregate_statistics)

if __name__ == "__main__":
    # index_ticker = "SPY" # SnP 500
    # index_ticker = "SPG"  # Simon Property Group
    index_ticker = "O" # Realty income corporations
    calculate_alpha_beta_statistics(index_ticker)
