import os
import yfinance as yf
import pandas as pd
import warnings
warnings.simplefilter(action="ignore", category=FutureWarning)

from re_analyzer.utility.utility import ALPHA_BETA_DATA_PATH
from re_analyzer.processors.real_estate_metrics_property_processor import MONTHS_IN_YEAR, DOWN_PAYMENT_PERCENTAGES, MIN_APR
from re_analyzer.analyzers.iterator import property_details_iterator, get_property_info_from_property_details


LOAN_TERM_YEARS = 30

def download_full_data(index_ticker, rf_ticker):
    # Download index data
    index_data = yf.download(index_ticker, progress=False)
    index_data.index = index_data.index.tz_localize(None)

    # Download risk-free rate data
    rf_data = yf.download(rf_ticker, progress=False)
    rf_data['Risk Free Rate'] = rf_data['Adj Close'].apply(lambda x: deannualize(x/100))
    rf_data.index = rf_data.index.tz_localize(None)

    return index_data, rf_data['Risk Free Rate']

def deannualize(annual_rate, periods=365):
    return (1 + annual_rate) ** (1/periods) - 1

def get_risk_free_rate_subset(df_rf, start_date):
    return df_rf[start_date:]

def get_stock_data_frame_subset(index_data, start_date):
    return index_data[start_date:]

def calculate_monthly_mortgage_payment(loan_amount, annual_interest_rate, loan_term_years):
    monthly_interest_rate = annual_interest_rate / MONTHS_IN_YEAR / 100
    n_payments = loan_term_years * MONTHS_IN_YEAR
    monthly_mortgage_payment = loan_amount * (monthly_interest_rate * (1 + monthly_interest_rate) ** n_payments) / ((1 + monthly_interest_rate) ** n_payments - 1)
    return monthly_mortgage_payment

def calculate_alpha_beta_for_property(zestimate_history, index_data, rf_data):
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

    index_df = get_stock_data_frame_subset(index_data, zestimate_history_df.index[0])
    df_rf = get_risk_free_rate_subset(rf_data, zestimate_history_df.index[0])

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
        lev_column_percentage = f'_{int(down_payment_percentage*100)}%_down' if down_payment_percentage != 1 else ''
        lev_column_name = f'Returns{lev_column_percentage}'
        aligned_df['Cumulative Mortgage Payments'] = monthly_mortgage_payment
        aligned_df['Cumulative Mortgage Payments'] = aligned_df['Cumulative Mortgage Payments'].cumsum()
        aligned_df['Equity'] = aligned_df['Home Price'] - loan_amount + down_payment_amount - aligned_df['Cumulative Mortgage Payments']
        aligned_df[lev_column_name] = aligned_df['Equity'].pct_change()
        return_columns.append(lev_column_name)

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


def load_existing_stats(file_path):
    """Load existing statistics from a CSV file, if available."""
    if os.path.exists(file_path):
        return pd.read_csv(file_path)
    return pd.DataFrame()


def calculate_alpha_beta_statistics(index_ticker, rf_ticker):
    existing_timeseries_df = load_existing_stats(ALPHA_BETA_DATA_PATH)
    existing_zpids = set(existing_timeseries_df['zpid']) if not existing_timeseries_df.empty else set()
    new_stats = []

    index_data, rf_data = download_full_data(index_ticker, rf_ticker)

    for property_details in property_details_iterator():
        property_info = get_property_info_from_property_details(property_details)
        if not property_info or property_info.get('zpid', 0) in existing_zpids:
            continue
        if 'zestimateHistory' not in property_details:
            continue
        zestimate_history = property_details['zestimateHistory']
        stats = calculate_alpha_beta_for_property(zestimate_history, index_data, rf_data)
        if not stats:
            continue
        zpid = property_info.get("zpid", 0)
        if not zpid:
            zpid = 0
        stats['zpid'] = int(zpid)
        new_stats.append(stats)
    
    if new_stats:
        timeseries_df = pd.DataFrame(new_stats)
        timeseries_df = pd.concat([existing_timeseries_df, timeseries_df], ignore_index=True)
    return timeseries_df

def alpha_beta_property_processing_pipeline():
    # index_ticker = "SPY" # SnP 500
    # index_ticker = "SPG"  # Simon Property Group
    index_ticker = "O" # Realty income corporations

    rf_ticker = "^IRX"
    return calculate_alpha_beta_statistics(index_ticker, rf_ticker)


if __name__ == "__main__":
    timeseries_df = alpha_beta_property_processing_pipeline()
