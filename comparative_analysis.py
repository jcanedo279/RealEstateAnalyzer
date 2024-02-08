import os
import math
import json
import glob
from datetime import datetime
import yfinance as yf
import pandas as pd
import numpy as np
from scipy.interpolate import interp1d
import matplotlib.pyplot as plt
import warnings
warnings.simplefilter(action="ignore", category=FutureWarning)

import sys


def deannualize(annual_rate, periods=365):
    return (1 + annual_rate) ** (1/periods) - 1

def get_risk_free_rate():
    # Download 3-month us treasury bills rates.
    annualized = yf.download("^IRX")["Adj Close"]
    daily = annualized.apply(deannualize)
    return pd.DataFrame({"annualized": annualized, "daily": daily})

# Function to fetch historical data for a given ticker
def fetch_stock_data_frame(ticker, starting_date):
    df = yf.download(ticker, start=starting_date)
    # Ensure the datetime index is timezone-naive for compatibility.
    df.index = df.index.tz_localize(None)
    return df

# Function to calculate moving averages
def calculate_moving_averages(data, windows=[30, 90]):
    moving_averages = {}
    for window in windows:
        moving_averages[f'{window}_day_MA'] = data['Adj Close'].rolling(window=window).mean()
    return pd.DataFrame(moving_averages, index=data.index)

# Function for linear interpolation of Zestimate data
def interpolate_timeseries(data):
    # Ensure the datetime index is timezone-naive for compatibility
    data['Date'] = pd.to_datetime(data['Date']).dt.tz_localize(None)
    data.set_index('Date', inplace=True)
    # Resample to daily frequency filling NaN values with linear interpolation
    return data.resample('D').mean().interpolate(method='linear')


# Main function
def main():
    ticker = "SPY" # SnP 500
    # ticker = "SPG"  # Simon Property Group
    # ticker = "O" # Realty income corporations

    annualized_risk_free_rate = get_risk_free_rate().iloc[-1]['daily']
    print(annualized_risk_free_rate)

    # Loop through each zip code.
    for zip_code_folder in glob.glob(os.path.join('PropertyDetails', '*')):
        zip_code = os.path.basename(zip_code_folder)
        
        # Process each JSON file within the zip code folder (each property).
        for json_file_path in glob.glob(os.path.join(zip_code_folder, '*_zestimate_history.json')):
            with open(json_file_path, 'r') as json_file:
                zestimate_history_data = json.load(json_file)
                # Extract and convert time series from json to a Pandas DF.
                zestimate_history = zestimate_history_data['data']['property']['homeValueChartData'][0]['points']
                zestimate_history_df = pd.DataFrame(zestimate_history)
                # Convert dates from UTC in ms to datetime objects.
                zestimate_history_df['x'] = pd.to_datetime(zestimate_history_df['x'], unit='ms')
                # Rename the columns appropriately.
                zestimate_history_df.rename(columns={'x': 'Date', 'y': 'Home Value'}, inplace=True)
                zestimate_history_df.set_index('Date', inplace=True)
                # Drop the specific date times and only keep the date itself.
                zestimate_history_df.index = pd.to_datetime(zestimate_history_df.index).normalize()

                index_df = fetch_stock_data_frame(ticker, zestimate_history_df.index[0])

                # index_df = 
                aligned_df = pd.merge_asof(zestimate_history_df, index_df, on='Date', direction='nearest')
                aligned_df.set_index('Date', inplace=True)

                aligned_df['Home Value Returns'] = aligned_df['Home Value'].pct_change()
                aligned_df['Stock Returns'] = aligned_df['Adj Close'].pct_change()
                aligned_df.dropna(inplace=True)

                print(aligned_df)

                cov_matrix = aligned_df[['Home Value Returns', 'Stock Returns']].cov()
                beta = cov_matrix.loc['Home Value Returns', 'Stock Returns'] / aligned_df['Stock Returns'].var()
                
                # Calculate Alpha using a risk free rate.
                alpha = (aligned_df['Home Value Returns'].mean() - annualized_risk_free_rate) - beta * (aligned_df['Stock Returns'].mean() - annualized_risk_free_rate)
            
                print(f"Alpha: {alpha}, Beta: {beta}")


    # priceHistory
    
    # print(data.tail())  # Display the last few rows to see the moving averages

if __name__ == "__main__":
    main()
