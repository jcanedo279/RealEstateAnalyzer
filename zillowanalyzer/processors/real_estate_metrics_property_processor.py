import os
import json
import glob
from datetime import datetime

from zillowanalyzer.scrapers.scraping_utility import *


# CONSTANTS
MONTHS_IN_YEAR = 12
# Unfortunately FL is the highest state :<, expect 2% on average.
AVERAGE_HOME_INSURANCE_RATE = 0.02
# Update from https://www.myfico.com/credit-education/calculators/loan-savings-calculator/ by FL state.
MIN_APR = 6.281
DOWN_PAYMENT_PERCENTAGES = [0.05, 0.1, 0.2]
# This was just manually calculated for a property work 725k (maybe a bit high, aim 400k), on 02/04/2024 (high interst), maybe calc when it gets lower.
DOWN_PAYMENT_TO_ANNUAL_PMI_RATE = { 0.05 : 0.0072, 0.1 : 0.0053, 0.15 : 0.0037, 0.2 : 0 }
# I've noticed that actual mortage rates are about 5% smaller.
PRINCIPAL_AND_INTEREST_DEDUCTION = 0.04
VACANCY_RATE = 0.075
MONTHLY_MAINTENANCE_RATE = 0.00017
MIN_PROPERTY_VALUE = 50000
FIXED_FEES = {
    'credit_report_fee' : 35,
    # Between 325 and 425.
    'appraisal_fee' : 375,
    # Fee paid to certified flood inspector -> determines if flood insurance is required if in flood zone.
    'flood_life_of_loan_fee' : 20,
    # Required by lender to insure buyer pays property taxes (usually pro-rated).
    # Between 50 and 100.
    'tax_service_fee' : 85,
    # Paid to title or escrow company for services when closing.
    'closing_escrow_and_settlement_fees' : 750,
    # Charged for government agencies to record your deed, mortgage, and other necessarily registered documents.
    'recording_fee' : 225,
    'survey_fee' : 300
}
CURRENT_MONTH = datetime.now().month
FIXED_FEE_TOTAL = sum(FIXED_FEES.values())


# Functions for fee calculations
def calculate_fees(purchase_price, down_payment):
    # Constants
    FLORIDA_ORIGINATION_FEE_RATE = 0.0075
    FLORIDA_LENDERS_TITLE_INSURANCE_BASE_FEE = 575
    FLORIDA_OWNERS_TITLE_INSURANCE_BASE_FEE = 40
    FLORIDA_OWNERS_TITLE_INSURANCE_RATE = 2.4411138235
    FLORIDA_MORTGAGES_TAX_RATE = 0.0035
    FLORIDA_DEEDS_TAX_RATE = 0.007
    FLORIDA_INTANGIBLE_TAX_RATE = 0.002

    # Calculate fees
    origination_fee = FLORIDA_ORIGINATION_FEE_RATE * purchase_price
    lenders_title_insurance_fee = FLORIDA_LENDERS_TITLE_INSURANCE_BASE_FEE + 5 * ((purchase_price - 100000) / 1000) if purchase_price >= 100000 else FLORIDA_LENDERS_TITLE_INSURANCE_BASE_FEE
    owners_title_insurance_fee = FLORIDA_OWNERS_TITLE_INSURANCE_BASE_FEE + FLORIDA_OWNERS_TITLE_INSURANCE_RATE * ((purchase_price - 100000) / 1000) if purchase_price >= 100000 else FLORIDA_OWNERS_TITLE_INSURANCE_BASE_FEE
    financed_amount = purchase_price * (1 - down_payment)
    state_and_stamps_tax = (FLORIDA_MORTGAGES_TAX_RATE + FLORIDA_DEEDS_TAX_RATE) * financed_amount
    intangible_tax = FLORIDA_INTANGIBLE_TAX_RATE * financed_amount

    total_fees = origination_fee + lenders_title_insurance_fee + owners_title_insurance_fee + state_and_stamps_tax + intangible_tax
    return total_fees

def calculate_monthly_costs(purchase_price, down_payment_percentage, annual_interest_rate, property_details, loan_term_years=30):
    # Calculate the down payment amount and financed amount
    down_payment_amount = purchase_price * down_payment_percentage
    loan_amount = purchase_price - down_payment_amount
    
    # Monthly mortgage payment calculation
    monthly_interest_rate = annual_interest_rate / MONTHS_IN_YEAR / 100
    n_payments = loan_term_years * MONTHS_IN_YEAR
    monthly_mortgage_payment = loan_amount * (monthly_interest_rate * (1 + monthly_interest_rate) ** n_payments) * (1-PRINCIPAL_AND_INTEREST_DEDUCTION) / ((1 + monthly_interest_rate) ** n_payments - 1)
    
    # Calculate monthly tax payments.
    tax_rate = property_details.get('propertyTaxRate', 0)
    if tax_rate:
        tax_rate *= 0.01
    if not tax_rate:
        tax_history = property_details['taxHistory']
        total_tax_rate = 0
        for record in tax_history:
            tax_paid, taxed_property_value = record['taxPaid'], record['value']
            if not tax_paid or not taxed_property_value:
                continue
            tax_rate = tax_paid / taxed_property_value
            total_tax_rate += tax_rate
    tax_rate = 0.02 if not tax_rate else tax_rate
    monthly_property_tax = tax_rate * purchase_price / MONTHS_IN_YEAR

    # Calculate montly homeowners insurance (not mortage insurance).
    annual_homeowners_insurance = property_details.get('annualHomeownersInsurance', 0)
    monthly_homeowners_insurance = AVERAGE_HOME_INSURANCE_RATE * purchase_price / MONTHS_IN_YEAR if not annual_homeowners_insurance else annual_homeowners_insurance / MONTHS_IN_YEAR

    # Calculate montly private mortgage insurance.
    monthly_pmi = purchase_price * DOWN_PAYMENT_TO_ANNUAL_PMI_RATE[down_payment_percentage] / MONTHS_IN_YEAR

    # Total monthly costs
    total_monthly_costs = monthly_mortgage_payment + monthly_pmi + monthly_property_tax + monthly_homeowners_insurance

    # Prepaids are typically paid upfront and not included in monthly costs but affect total cash invested.
    prepaid_real_estate_tax_escrow = monthly_property_tax * CURRENT_MONTH
    prepaid_insurance_escrow = monthly_homeowners_insurance * CURRENT_MONTH
    total_prepaid_costs = prepaid_real_estate_tax_escrow + prepaid_insurance_escrow

    # Total cash invested at purchase (down payment + fixed fees + prepaids, not including in monthly costs)
    total_cash_invested = down_payment_amount + FIXED_FEE_TOTAL + total_prepaid_costs + calculate_fees(purchase_price, down_payment_percentage)

    return total_monthly_costs, total_cash_invested, total_prepaid_costs

def calculate_monthly_mortgage(purchase_price, down_payment_percentage, annual_interest_rate, loan_term_years):
    down_payment = purchase_price * (down_payment_percentage / 100)
    loan_amount = purchase_price - down_payment
    monthly_interest_rate = annual_interest_rate / 12 / 100
    n_payments = loan_term_years * 12

    monthly_payment = loan_amount * (monthly_interest_rate * (1 + monthly_interest_rate) ** n_payments) / ((1 + monthly_interest_rate) ** n_payments - 1)
    return monthly_payment

def calculate_real_estate_metrics(base_path=PROPERTY_DETAILS_PATH):
    results = {}
    annual_debt_service = 0

    results = []

    # Loop through each zip code.
    for zip_code_folder in glob.glob(os.path.join(base_path, '*')):
        zip_code = os.path.basename(zip_code_folder)
        
        # Process each JSON file within the zip code folder (each property).
        for json_file_path in glob.glob(os.path.join(zip_code_folder, '*_property_details.json')):
            with open(json_file_path, 'r') as json_file:
                property_details = json.load(json_file)
                property_data = property_details['props']['pageProps']['componentProps']['gdpClientCache']
                first_key = next(iter(property_data))
                property_info = property_data[first_key].get("property", None)
                
                if not property_info or property_info.get('price', 0) < MIN_PROPERTY_VALUE:
                    continue

                rent_estimate = property_info.get('rentZestimate', 0)
                rent_estimate = 0 if not rent_estimate else rent_estimate
                purchase_price = property_info.get('price', 0)

                metrics = {
                    'zpid' : property_info.get('zpid', 0),
                    'street_address': property_info.get('streetAddress', 'No Property Address Located'),
                    'zip_code': zip_code,
                    'purchase_price': purchase_price,
                    'gross_rent_multiplier' : purchase_price / (MONTHS_IN_YEAR * rent_estimate) if rent_estimate != 0 else 'inf',
                }

                for down_payment_percentage in DOWN_PAYMENT_PERCENTAGES:
                    down_payment_literal = f"{down_payment_percentage * 100}% Down"
                    total_monthly_costs, total_cash_invested, total_prepaid_costs = calculate_monthly_costs(purchase_price, down_payment_percentage, MIN_APR, property_info)
                    metrics.update({
                        f'break_even_ratio {down_payment_literal}' : (total_monthly_costs * 12 + annual_debt_service) / (MONTHS_IN_YEAR * rent_estimate) if rent_estimate != 0 else 'inf',
                        f'CoC_no_prepaids {down_payment_literal}' : MONTHS_IN_YEAR * (rent_estimate - total_monthly_costs) / total_cash_invested,
                        f'CoC {down_payment_literal}' : MONTHS_IN_YEAR * (rent_estimate - total_monthly_costs) / (total_cash_invested - total_prepaid_costs),
                        f'adj_CoC_no_prepaids {down_payment_literal}' : MONTHS_IN_YEAR * (rent_estimate * (1 - VACANCY_RATE) - total_monthly_costs - (MONTHLY_MAINTENANCE_RATE * purchase_price)) / total_cash_invested,
                        f'adj_CoC {down_payment_literal}' : MONTHS_IN_YEAR * (rent_estimate * (1 - VACANCY_RATE) - total_monthly_costs - (MONTHLY_MAINTENANCE_RATE * purchase_price)) / (total_cash_invested - total_prepaid_costs),
                        f'cap_rate {down_payment_literal}' : MONTHS_IN_YEAR * (rent_estimate - total_monthly_costs) / purchase_price,
                        f'adj_cap_rate {down_payment_literal}' : MONTHS_IN_YEAR * (rent_estimate * (1 - VACANCY_RATE) - total_monthly_costs - (MONTHLY_MAINTENANCE_RATE * purchase_price)) / purchase_price,
                    })
                results.append(metrics)

    # Sort the list of dictionaries for the current zip code by 'adj_CoC' with some percentage down, in descending order
    results.sort(key=lambda x: x['adj_CoC 5.0% Down'], reverse=True)

    # Save the results to a JSON file.
    save_json(results, f'{DATA_PATH}/processed_property_metric_results.json')


results = calculate_real_estate_metrics()
