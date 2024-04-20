import math
import os
import json

from zillowanalyzer.utility.utility import get_abs_path, load_json, ZILLOW_ANALYZER_PATH
from zillowanalyzer.analyzers.preprocessing import load_data
from zillowanalyzer.analyzers.iterator import get_property_info_from_property_details


BACKEND_PROPERTIES_DF = load_data(drop_street_address=False).round(2)

region_path = os.path.join(ZILLOW_ANALYZER_PATH, "application", "deal_analysis_application", "data", "regions.json")
REGION_TO_ZIP_CODE = {region: set(zip_codes) for region, zip_codes in load_json(region_path).items()}

TARGET_COLUMNS = ['Image', 'Save', 'City', 'Rental Income (5% Down)', 'Rent Estimate', 'Price', 'Breakeven Price (5% Down)', 'Competative Price (5% Down)', 'Is Breakeven Price Offending', 'Adjusted CoC (5% Down)', 'Year Built', 'Home Type', 'Bedrooms', 'Bathrooms']
BACKEND_COL_NAME_TO_FRONTEND_COL_NAME = {
    "city": {
        "name": "City"},
    "street_address": {
        "name": "Property Address"},
    "purchase_price": {
        "name": "Price"},
    "restimate": {
        "name": "Rent Estimate",
        "description": "Projected montly rental."},
    "year_built": {
        "name": "Year Built"},
    "home_type": {
        "name": "Home Type",
        "description": "The listed housing type (i.e. SingleFamily or TownHouse)."},
    "bedrooms": {
        "name": "Bedrooms"},
    "bathrooms": {
        "name": "Bathrooms"},
    "zip_code": {
        "name": "Zip Code"},
    "gross_rent_multiplier": {
        "name": "Gross Rent Multiplier",
        "description:": "The ratio (PurchasePrice)/(RentEstimate). A lower ratio is typically better for the buyer as it indicates that the home's rental is high relative to its price."},
    "page_view_count": {
        "name": "Times Viewed"},
    "favorite_count": {
        "name": "Favorite Count"},
    "days_on_zillow": {
        "name": "Days on Zillow"},
    "property_tax_rate": {
        "name": "Tax Rate (%)",
        "description": "The historical annual percentage of the home's price which is charged as property taxes."},
    "living_area": {
        "name": "Living Area (sq ft)",
        "description": "The size of the home's living area in square feet."},
    "lot_size": {
        "name": "Lot Size (sq ft)",
        "description": "The size of the home's lot in square feet."},
    "mortgage_rate": {
        "name": "Mortgage Rate",
        "description": "The projected APR for a mortgage on this property."},
    "homeowners_insurance": {
        "name": "Home Insurance",
        "description": "The monthly homeowners insurance which is charged historically on an annual basis."},
    "monthly_hoa": {
        "name": "HOA Fee",
        "description": "The historical monthly HOA fee which is collected by the neighborhood."},
    "home_features_score": {
        "name": "Home Features Score",
        "description": "A generated score in the interval [-1,1], which represents how many 'important' features a home has, and is exponentially correlated with its price."},
    "is_waterfront": {
        "name": "Waterfront",
        "description": "Whether a home is a waterfront property or not."},
}
BACKEND_COL_NAME_TO_DYNAMIC_FRONTEND_COL_NAME = {
    # Down payment based keys.
    "adj_CoC": {
        "name": "Adjusted CoC",
        "description": "The annual 'cash on cash' returns, i.e. the rental income as a fraction of the cash invested, adjusted for average vacancy and maintenance rates. Annualized for comparison."},
    "rental_income": {
        "name": "Rental Income",
        "description": "The monthly rental income, the projected rental estimate minus all expenses (i.e. mortage, HOA, insurance, taxes, etc...)."},
    "Beta": {
        "name": "Beta",
        "description": "Beta is a measure of the property's price volatility compared to the broader market, using the property's alpha (see column) and the rolling risk free return (3 month treasury). A beta above 1 means the price is more volatile than the market, a beta of 1 means the price changes with the market, a beta between 0 and 1 means its less volatile than the market, a beta of 0 means the price does not change with the market (such as cash), finally a negative beta indicates an inverse relation to the market."},
    "Alpha": {
        "name": "Alpha",
        "description": "Alpha is the active return on an investment compared to the broader market. A positive alpha indicates that a property has increased in value more than the market average, often due to factors like location, improvements, or market dynamics. A negative alpha suggests it has underperformed the market benchmark."},
    "breakeven_price": {
        "name": "Breakeven Price",
        "description": "The purchase price at which the rental income  is zero, if the breakeven price is above the asking price this is just the listing price. Adjusted for the "},
    "is_breaven_price_offending": {
        "name": "Is Breakeven Price Offending",
        "description": "Whether the breakeven price is an offending offer (less than 80% of the listing price)."},
    "snp_equivalent_price": {
        "name": "Competative Price",
        "description": "The purchase price at which the annualized rental income is comporable to half the historical SnP 500 returns."},
    "CoC_no_prepaids": {
        "name": "CoC w/o Prepaids",
        "description": "The annual 'cash on cash' returns, i.e. the rental income as a fraction of the cash invested (without prepaids). Annualized for comparison."},
    "CoC": {
        "name": "CoC",
        "description": "The annual 'cash on cash' returns, i.e. the rental income as a fraction of the cash invested. Annualized for comparison."},
    "adj_CoC_no_prepaids": {
        "name": "Adjusted CoC w/o Prepaids",
        "description": "The annual 'cash on cash' returns, i.e. the rental income as a fraction of the cash invested (without prepaids), adjusted for average vacancy and maintenance rates. Annualized for comparison."},
    "cap_rate": {
        "name": "Cap Rate",
        "description": "The rental income as a fraction of the purchase price."},
    "adj_cap_rate":  {
        "name": "Adjusted Cap Rate",
        "description": "The rental income as a fraction of the purchase price. The rental income being adjusted for vacancy and maintenance rates."},
}

# Function to construct a dictionary to map from the input names ot the descriptive ones.
def create_rename_dict():
    rename_dict = {}
    for old_name, props in BACKEND_COL_NAME_TO_FRONTEND_COL_NAME.items():
        rename_dict[old_name] = props['name']
    
    for old_name, props in BACKEND_COL_NAME_TO_DYNAMIC_FRONTEND_COL_NAME.items():
        rename_dict[old_name] = props['name']
        rename_dict[f"{old_name}_5%_down"] = f"{props['name']} (5% Down)"
    
    return rename_dict

def create_description_dict():
    description_dict = {}
    for props in BACKEND_COL_NAME_TO_FRONTEND_COL_NAME.values():
        if 'description' in props:
            description_dict[props['name']] = props['description']
    
    for props in BACKEND_COL_NAME_TO_DYNAMIC_FRONTEND_COL_NAME.values():
        if 'description' in props:
            description_dict[props['name']] = props['description']
            description_dict[f"{props['name']} (5% Down)"] = f"Given a 5% down payment... {props['description']}"
    
    return description_dict


def properties_df_from_search_request_data(request_data):
    region = request_data.get('region')
    home_type = request_data.get('home_type')
    year_built = int(request_data.get('year_built'))
    max_price = float(request_data.get('max_price'))
    city = request_data.get('city')
    is_waterfront = bool(request_data.get('is_waterfront'))
    is_cashflowing = bool(request_data.get('is_cashflowing'))

    properties_df = BACKEND_PROPERTIES_DF.copy()
    if region != "ANY_AREA":
        properties_df = properties_df[properties_df['zip_code'].isin(REGION_TO_ZIP_CODE[region])]
    if home_type != "ANY":
        properties_df = properties_df[properties_df['home_type'] == home_type]
    if year_built:
        properties_df = properties_df[properties_df['year_built'] >= year_built]
    if max_price:
        properties_df = properties_df[properties_df['purchase_price'] <= max_price]
    if is_waterfront:
        properties_df = properties_df[properties_df['is_waterfront'] > 0.0]
    if is_cashflowing:
        properties_df = properties_df[properties_df['adj_CoC_5%_down'] >= 0.0]
    if city:
        properties_df = properties_df[properties_df['city'] == city.title()]

    properties_df['is_waterfront'] = properties_df['is_waterfront'].apply(lambda x: 'True' if x > 0.0 else 'False')
    return properties_df

def properties_response_from_properties_df(properties_df, num_properties_per_page=1, page=1, saved_zpids={}):
    num_properties_found = properties_df.shape[0]
    properties_df = properties_df.sort_values(by='adj_CoC_5%_down', ascending=False)
    # Calculate the total number of pages of listings in the backend to send to the frontend for the back/next buttons.
    total_pages = math.ceil(num_properties_found / num_properties_per_page)
    # We sort by CoC before filtering.
    start_property_index, stop_property_index = (page-1)*num_properties_per_page, page*num_properties_per_page
    properties_df = properties_df[start_property_index:stop_property_index]

    for zpid, filtered_property in properties_df.iterrows():
        zip_code, zpid = int(filtered_property['zip_code']), int(zpid)
        property_details_path = get_abs_path(f'Data/PropertyDetails/{zip_code}/{zpid}_property_details.json')
        if not os.path.exists(property_details_path):
            properties_df.drop(zpid, axis=0, inplace=True)
        with open(property_details_path, 'r') as json_file:
            property_details = json.load(json_file)
        if 'props' in property_details:
            property_info = get_property_info_from_property_details(property_details)
            image_url = property_info['originalPhotos'][0]['mixedSources']['jpeg'][1]['url']
            properties_df.loc[zpid, 'Image'] = image_url
            properties_df.loc[zpid, 'property_url'] = 'https://zillow.com' + property_info['hdpUrl']
        else:
            properties_df.loc[zpid, 'Image'] = None
            properties_df.loc[zpid, 'property_url'] = None
        properties_df.loc[zpid, 'Save'] = zpid in saved_zpids
        properties_df.loc[zpid, 'zpid'] = zpid

    # Add the zpid as a column.
    properties_df.rename(columns=create_rename_dict(), inplace=True)

    if num_properties_found:
        ordered_cols = TARGET_COLUMNS + [col for col in properties_df.columns if col not in set(TARGET_COLUMNS)]
        ordered_properties_data = properties_df[ordered_cols].to_json(orient="records")
    else:
        ordered_properties_data = '{}'
    return {
        "properties": json.loads(ordered_properties_data),
        "descriptions": create_description_dict(),
        "total_properties": num_properties_found,
        "total_pages": total_pages,
    }
