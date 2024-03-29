import os
import csv
from datetime import datetime

from zillowanalyzer.utility.utility import PROPERTY_DETAILS_PATH, REAL_ESTATE_METRICS_DATA_PATH
from zillowanalyzer.analyzers.iterator import property_details_iterator, get_property_info_from_property_details


# CONSTANTS
MONTHS_IN_YEAR = 12
# Unfortunately FL is the highest state :<, expect 2% on average.
AVERAGE_HOME_INSURANCE_RATE = 0.02
# Update from https://www.myfico.com/credit-education/calculators/loan-savings-calculator/ by FL state.
MIN_APR = 6.281
DOWN_PAYMENT_PERCENTAGES = [0.05, 1.0]
# This was just manually calculated for a property work 725k (maybe a bit high, aim 400k), on 02/04/2024 (high interst), maybe calc when it gets lower.
DOWN_PAYMENT_TO_ANNUAL_PMI_RATE = { 0.05 : 0.0072, 1.0 : 0 }
# I've noticed that actual mortage rates are about 5% smaller.
PRINCIPAL_AND_INTEREST_DEDUCTION = 0.04
VACANCY_RATE = 0.1
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

def calculate_monthly_mortgage_rate(mortgage_rate, loan_term_years=30):
    monthly_interest_rate = mortgage_rate / MONTHS_IN_YEAR / 100
    n_payments = loan_term_years * MONTHS_IN_YEAR
    return (monthly_interest_rate * (1 + monthly_interest_rate) ** n_payments) * (1-PRINCIPAL_AND_INTEREST_DEDUCTION) / ((1 + monthly_interest_rate) ** n_payments - 1)

def calculate_monthly_property_tax_rate(property_info):
    tax_rate = property_info.get('propertyTaxRate', 0)
    if tax_rate:
        tax_rate *= 0.01
    if not tax_rate:
        tax_history = property_info['taxHistory']
        total_tax_rate = 0
        for record in tax_history:
            tax_paid, taxed_property_value = record['taxPaid'], record['value']
            if not tax_paid or not taxed_property_value:
                continue
            tax_rate = tax_paid / taxed_property_value
            total_tax_rate += tax_rate
    tax_rate = 0.02 if not tax_rate else tax_rate
    return tax_rate  / MONTHS_IN_YEAR

def calculate_monthly_homeowners_insurance_rate(property_info, purchase_price):
    annual_homeowners_insurance = property_info.get('annualHomeownersInsurance', 0)
    return AVERAGE_HOME_INSURANCE_RATE / MONTHS_IN_YEAR if not annual_homeowners_insurance else annual_homeowners_insurance / (MONTHS_IN_YEAR * purchase_price)

def purchase_price_with_cash_flow_percentage(property_info, purchase_price, rent_estimate, monthly_hoa, down_payment_percentage, mortgage_rate, cash_flow_rate=0):
    cost_rate = MONTHLY_MAINTENANCE_RATE + (DOWN_PAYMENT_TO_ANNUAL_PMI_RATE[down_payment_percentage] / MONTHS_IN_YEAR) * (1 - down_payment_percentage) + calculate_monthly_mortgage_rate(mortgage_rate) + calculate_monthly_property_tax_rate(property_info) + calculate_monthly_homeowners_insurance_rate(property_info, purchase_price)
    return (rent_estimate * (1 - VACANCY_RATE) - monthly_hoa) / (cost_rate + (cash_flow_rate / MONTHS_IN_YEAR))


def calculate_monthly_costs(purchase_price, down_payment_percentage, mortgage_rate, monthly_hoa, property_info):
    # Calculate the down payment amount and financed amount
    down_payment_amount = purchase_price * down_payment_percentage
    loan_amount = purchase_price - down_payment_amount
    
    # Monthly mortgage payment calculation
    monthly_mortgage_payment = loan_amount * calculate_monthly_mortgage_rate(mortgage_rate)
    
    # Calculate monthly tax payments.
    monthly_property_tax = purchase_price * calculate_monthly_property_tax_rate(property_info)

    # Calculate montly homeowners insurance (not mortage insurance).
    monthly_homeowners_insurance = calculate_monthly_homeowners_insurance_rate(property_info, purchase_price) * purchase_price

    # Calculate montly private mortgage insurance.
    monthly_pmi = purchase_price * DOWN_PAYMENT_TO_ANNUAL_PMI_RATE[down_payment_percentage] / MONTHS_IN_YEAR

    # Total monthly costs
    total_monthly_costs = monthly_mortgage_payment + monthly_pmi + monthly_property_tax + monthly_homeowners_insurance + monthly_hoa

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

def real_estate_metrics_property_processing_pipeline():
    results = {}
    annual_debt_service = 0

    results = []
    for property_details in property_details_iterator():
        property_info = get_property_info_from_property_details(property_details)
        if not property_info:
            continue
        zip_code = property_info.get('zipcode', 0)

        rent_estimate = property_info.get('rentZestimate', 0)
        rent_estimate = 0 if not rent_estimate else rent_estimate
        purchase_price = property_info.get('price', 1)
        if not purchase_price:
            purchase_price = 1
        year_built = property_info.get('yearBuilt', 1960)
        bedrooms = property_info.get('bedrooms', 0)
        if not bedrooms:
            bedrooms = 0
        bathrooms = property_info.get('bathrooms', 0)
        if not bathrooms:
            bathrooms = 0
        page_view_count = property_info.get('pageViewCount', 0)
        if not page_view_count:
            page_view_count = 0
        favorite_count = property_info.get('favoriteCount', 0)
        if not favorite_count:
            favorite_count = 0
        time_on_zillow = property_info.get('timeOnZillow', '0 days')
        property_tax_rate = property_info.get('propertyTaxRate', 0)
        living_area = property_info.get('livingArea', 0)
        if not living_area:
            living_area = 0
        lot_size = property_info.get('lotSize', 0)
        if not lot_size:
            lot_size = living_area
        home_type = property_info.get('homeType', 'SINGLE_FAMILY')
        mortgage_rate = property_info.get('mortgageRates', { "thirtyYearFixedRate": 6 })
        if mortgage_rate:
            mortgage_rate = mortgage_rate.get('thirtyYearFixedRate', 6)
        if not mortgage_rate:
            mortgage_rate = 6
        annual_homeowners_insurance = property_info.get('annualHomeownersInsurance', 0)
        days_on_zillow = time_on_zillow.split()[0]
        if time_on_zillow.split()[1] in {"day", "hours"}:
            days_on_zillow = 1
        monthly_hoa = property_info.get("monthlyHoaFee", 0)
        if not monthly_hoa:
            monthly_hoa = 0

        metrics = {
            'zpid' : property_info.get('zpid', 0),
            'street_address': property_info.get('streetAddress', 'No Property Address Located'),
            'zip_code': zip_code,
            'purchase_price': purchase_price,
            'restimate': rent_estimate,
            'gross_rent_multiplier' : purchase_price / (MONTHS_IN_YEAR * rent_estimate) if rent_estimate != 0 else 'inf',
            'year_built': year_built,
            'bedrooms': bedrooms, 'bathrooms': bathrooms,
            'page_view_count': page_view_count, 'favorite_count': favorite_count,
            'days_on_zillow': days_on_zillow,
            'property_tax_rate': property_tax_rate,
            'living_area': living_area,
            'lot_size': lot_size,
            'home_type': home_type,
            'mortgage_rate': mortgage_rate,
            'annual_homeowners_insurance': annual_homeowners_insurance,
            'monthly_hoa': monthly_hoa,
            'city': property_info.get('city', '')
        }

        for down_payment_percentage in DOWN_PAYMENT_PERCENTAGES:
            down_payment_literal = f" {down_payment_percentage * 100}% Down" if down_payment_percentage != 1 else ""
            total_monthly_costs, total_cash_invested, total_prepaid_costs = calculate_monthly_costs(purchase_price, down_payment_percentage, mortgage_rate, monthly_hoa, property_info)
            monthly_rental_income = rent_estimate * (1 - VACANCY_RATE) - total_monthly_costs - (MONTHLY_MAINTENANCE_RATE * purchase_price)
            breakeven_purchase_price = purchase_price_with_cash_flow_percentage(property_info, purchase_price, rent_estimate, monthly_hoa, down_payment_percentage, mortgage_rate)
            target_purchase_price = purchase_price_with_cash_flow_percentage(property_info, purchase_price, rent_estimate, monthly_hoa, down_payment_percentage, mortgage_rate, cash_flow_rate=0.05)
            metrics.update({
                f'breakeven_price{down_payment_literal}' : breakeven_purchase_price,
                f'is_breaven_price_offending{down_payment_literal}' : abs(purchase_price - breakeven_purchase_price) > 0.2 * purchase_price,
                f'snp_equivalent_price{down_payment_literal}' : target_purchase_price,
                f'CoC_no_prepaids{down_payment_literal}' : MONTHS_IN_YEAR * (monthly_rental_income) / total_cash_invested,
                f'CoC{down_payment_literal}' : MONTHS_IN_YEAR * (monthly_rental_income) / (total_cash_invested - total_prepaid_costs),
                f'adj_CoC_no_prepaids{down_payment_literal}' : MONTHS_IN_YEAR * (monthly_rental_income) / total_cash_invested,
                f'adj_CoC{down_payment_literal}' : MONTHS_IN_YEAR * (monthly_rental_income) / (total_cash_invested - total_prepaid_costs),
                f'rental_income{down_payment_literal}' : monthly_rental_income,
                f'cap_rate{down_payment_literal}' : MONTHS_IN_YEAR * (monthly_rental_income) / purchase_price,
                f'adj_cap_rate{down_payment_literal}' : MONTHS_IN_YEAR * (monthly_rental_income) / purchase_price,
            })
        results.append(metrics)

    # Sort the list of dictionaries for the current zip code by 'adj_CoC' with some percentage down, in descending order
    sorted_metrics = sorted(results, key=lambda x: x['adj_CoC 5.0% Down'], reverse=True)

    # Open CSV file for writing
    csv_columns = results[0].keys()
    with open(REAL_ESTATE_METRICS_DATA_PATH, mode='w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=csv_columns)
        writer.writeheader()
        
        # Write the sorted metrics to the CSV file
        total_count = 0
        for metrics in sorted_metrics:
            total_count += 1
            writer.writerow(metrics)


if __name__ == '__main__':
    results = real_estate_metrics_property_processing_pipeline()
