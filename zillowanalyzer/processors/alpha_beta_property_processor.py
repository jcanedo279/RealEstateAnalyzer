import os
import json
import glob
import yfinance as yf
import pandas as pd
import warnings
warnings.simplefilter(action="ignore", category=FutureWarning)

from zillowanalyzer.scrapers.scraping_utility import *
from zillowanalyzer.processors.real_estate_metrics_property_processor import *

import sys

LOAN_TERM_YEARS = 30


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

def calculate_monthly_mortgage_payment(loan_amount, annual_interest_rate, loan_term_years):
    monthly_interest_rate = annual_interest_rate / MONTHS_IN_YEAR / 100
    n_payments = loan_term_years * MONTHS_IN_YEAR
    monthly_mortgage_payment = loan_amount * (monthly_interest_rate * (1 + monthly_interest_rate) ** n_payments) / ((1 + monthly_interest_rate) ** n_payments - 1)
    return monthly_mortgage_payment

def calculate_alpha_beta_for_property(zestimate_history, index_ticker):
    # Extract and convert time series from json to a Pandas DF.
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
    zestimate_history_df['Home Price Returns'] = zestimate_history_df['Home Price'].pct_change()

    index_df = fetch_stock_data_frame(index_ticker, zestimate_history_df.index[0])
    df_rf = fetch_risk_free_rate(zestimate_history_df.index[0])

    aligned_df = pd.merge_asof(zestimate_history_df, index_df, on='Date', direction='nearest')
    aligned_df = pd.merge_asof(aligned_df, df_rf, on='Date', direction='nearest')
    aligned_df.set_index('Date', inplace=True)

    aligned_df['Stock Returns'] = aligned_df['Adj Close'].pct_change()

    initial_home_price = zestimate_history_df['Home Price'].iloc[0]
    # Calculate returns based on leveraged investment
    return_columns = []
    for down_payment_percentage in DOWN_PAYMENT_PERCENTAGES:
        down_payment_amount = initial_home_price * down_payment_percentage
        loan_amount = initial_home_price - down_payment_amount
        monthly_mortgage_payment = calculate_monthly_mortgage_payment(loan_amount, MIN_APR, LOAN_TERM_YEARS)

        # Calculate leveraged returns
        lev_column_name = f'Leveraged Returns {down_payment_percentage*100}% Down'
        aligned_df['Cumulative Mortgage Payments'] = monthly_mortgage_payment
        aligned_df['Cumulative Mortgage Payments'] = aligned_df['Cumulative Mortgage Payments'].cumsum()
        aligned_df['Equity'] = aligned_df['Home Price'] - loan_amount + down_payment_amount - aligned_df['Cumulative Mortgage Payments']
        aligned_df[lev_column_name] = aligned_df['Equity'].pct_change()
        return_columns.append(lev_column_name)
    return_columns.append('Home Price Returns')

    # Get rid of the first row since it does not have a percent change reference.
    aligned_df.dropna(inplace=True)
    
    processed_data = {}
    for leveraged_column in return_columns:
        cov_matrix = aligned_df[[leveraged_column, 'Stock Returns']].cov()
        beta = cov_matrix.loc[leveraged_column, 'Stock Returns'] / aligned_df['Stock Returns'].var()
        # Calculate Alpha using a risk free rate and the CAPM formula.
        alpha = (aligned_df[leveraged_column] - aligned_df['Risk Free Rate']).mean() - beta * (aligned_df['Stock Returns'] - aligned_df['Risk Free Rate']).mean()
        
        processed_data[leveraged_column.replace('Returns', "Beta")] = beta
        processed_data[leveraged_column.replace('Returns', "Alpha")] = alpha
    return processed_data



def calculate_alpha_beta_statistics(index_ticker):
    alpha_beta_values = []

    search_results = load_json(SEARCH_RESULTS_PROCESSED_PATH)
    search_results, num_search_results = load_json(SEARCH_RESULTS_PROCESSED_PATH), len(search_results)
    for search_result_ind, search_result in enumerate(search_results):
        zip_code, zpid = search_result['zip_code'], search_result['zpid']

        property_data = None
        with open(f'{PROPERTY_DETAILS_PATH}/{zip_code}/{zpid}_property_details.json') as json_file:
            property_data = json.load(json_file)
        if not property_data:
            continue
        print(f'Processing property: {zpid} in zip_code: {zip_code}... property number: [{search_result_ind} / {num_search_results}]', end='         \r')
        
        zestimate_history = property_data['zestimateHistory']
        alpha_beta_value = calculate_alpha_beta_for_property(zestimate_history, index_ticker)
        if not alpha_beta_value:
            continue
        alpha_beta_value['zip_code'] = zip_code
        alpha_beta_value['zpid'] = zpid
        alpha_beta_values.append(alpha_beta_value)
        

    alpha_beta_df = pd.DataFrame(alpha_beta_values)
    alpha_beta_df.sort_values('Home Price Alpha', ascending=False, inplace=True)
    # Calculate aggregate statistics.
    alpha_beta_df.to_csv(f'{DATA_PATH}/AlphaBetaStats.csv', index=False)
    aggregate_statistics = alpha_beta_df.describe()

    print(aggregate_statistics)

if __name__ == "__main__":
    # index_ticker = "SPY" # SnP 500
    # index_ticker = "SPG"  # Simon Property Group
    index_ticker = "O" # Realty income corporations
    calculate_alpha_beta_statistics(index_ticker)
