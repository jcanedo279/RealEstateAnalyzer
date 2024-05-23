import pandas as pd

from re_analyzer.analyzers.iterator import property_details_iterator, get_property_info_from_property_details


# CONSTANTS
MONTHS_IN_YEAR = 12
# Update from https://www.myfico.com/credit-education/calculators/loan-savings-calculator/ by FL state.
MIN_APR = 6.281
DOWN_PAYMENT_PERCENTAGES = [0.05, 1.0]


def real_estate_metrics_property_processing_pipeline():
    results = {}

    results = []
    for property_details in property_details_iterator():
        property_info = get_property_info_from_property_details(property_details)
        if not property_info:
            continue
        zpid = property_info.get('zpid', 0)
        if not zpid:
            zpid = 0
        zip_code = property_info.get('zipcode', 0)
        if not zip_code:
            zip_code = 0
        monthly_restimate = property_info.get('rentZestimate', 0)
        if not monthly_restimate:
            monthly_restimate = 0
        purchase_price = property_info.get('price', 1)
        if not purchase_price:
            purchase_price = 1
        year_built = property_info.get('yearBuilt', 1960)
        if not year_built:
            year_built = 1960
        bedrooms = property_info.get('bedrooms', 0)
        if not bedrooms:
            bedrooms = 0
        bathrooms = property_info.get('bathrooms', 0)
        if not bathrooms:
            bathrooms = 0
        time_on_zillow = property_info.get('timeOnZillow', '0 days')
        if not time_on_zillow:
            time_on_zillow = '0 days'
        if time_on_zillow.split()[1] in {"day", "hours"}:
            days_on_zillow = 1
        else:
            days_on_zillow = time_on_zillow.split()[0]
        annual_property_tax_rate = property_info.get('propertyTaxRate', 0)
        if not annual_property_tax_rate:
            annual_property_tax_rate = 0
        living_area = property_info.get('livingArea', 0)
        if not living_area:
            living_area = 0
        lot_size = property_info.get('lotSize', 0)
        if not lot_size:
            lot_size = living_area
        home_type = property_info.get('homeType', 'SINGLE_FAMILY')
        if not home_type:
            home_type = 'SINGLE_FAMILY'
        annual_mortgage_rate = property_info.get('mortgageRates', { "thirtyYearFixedRate": 6 })
        if annual_mortgage_rate:
            annual_mortgage_rate = annual_mortgage_rate.get('thirtyYearFixedRate', 6)
        if not annual_mortgage_rate:
            annual_mortgage_rate = 6
        annual_homeowners_insurance = property_info.get('annualHomeownersInsurance', 0)
        if not annual_homeowners_insurance:
            annual_homeowners_insurance = 0
        monthly_homeowners_insurance = annual_homeowners_insurance / MONTHS_IN_YEAR
        monthly_hoa = property_info.get("monthlyHoaFee", 0)
        if not monthly_hoa:
            monthly_hoa = 0

        try:
            image_url = property_info['originalPhotos'][0]['mixedSources']['jpeg'][1]['url']
        except (KeyError, IndexError):
            image_url = ''
        property_url = 'https://zillow.com' + property_info.get('hdpUrl', '')

        metrics = {
            'zpid' : int(zpid),
            'street_address': property_info.get('streetAddress', 'No Property Address Located'),
            'zip_code': int(zip_code),
            'purchase_price': float(purchase_price),
            'monthly_restimate': float(monthly_restimate),
            'gross_rent_multiplier' : float(purchase_price / (MONTHS_IN_YEAR * monthly_restimate)) if monthly_restimate != 0 else -1,
            'year_built': int(year_built),
            'bedrooms': int(bedrooms), 'bathrooms': int(bathrooms),
            'annual_property_tax_rate': float(annual_property_tax_rate),
            'living_area': int(living_area), 'lot_size': int(lot_size),
            'home_type': str(home_type),
            'annual_mortgage_rate': float(annual_mortgage_rate),
            'monthly_homeowners_insurance': float(monthly_homeowners_insurance),
            'monthly_hoa': float(monthly_hoa),
            'city': property_info.get('city', ''),
            'image_url': image_url,
            'property_url': property_url
        }
        results.append(metrics)

    # Convert the list of dictionaries to a DataFrame
    metrics_df = pd.DataFrame(results)

    # Sort the DataFrame by 'restimate' column in descending order
    # metrics_df = metrics_df.sort_values(by='restimate', ascending=False)

    return metrics_df


if __name__ == '__main__':
    real_estate_metrics_property_processing_pipeline()
