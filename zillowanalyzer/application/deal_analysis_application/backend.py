import logging
import math
import os
import json

from flask import Flask, render_template, request, Response, jsonify

from zillowanalyzer.utility.utility import get_abs_path, load_json, ZILLOW_ANALYZER_PATH
from zillowanalyzer.analyzers.preprocessing import load_data, preprocess_dataframe, FilterMethod
from zillowanalyzer.analyzers.iterator import get_property_info_from_property_details


app = Flask(__name__)
app.logger.setLevel(logging.INFO)

BACKEND_PROPERTIES_DF = load_data().round(2)

region_path = os.path.join(ZILLOW_ANALYZER_PATH, "application", "deal_analysis_application", "data", "regions.json")
REGION_TO_ZIP_CODE = {region: set(zip_codes) for region, zip_codes in load_json(region_path).items()}

TARGET_COLUMNS = ['Image', 'City', 'Rental Income (5% Down)', 'Rent Estimate', 'Price', 'Breakeven Price (5% Down)', 'S&P Competative Price (5% Down)', 'Is Breakeven Price Offending', 'Adjusted Cash on Cash (5% Down)', 'Year Built', 'Home Type', 'Bedrooms', 'Bathrooms']
BACKEND_COL_NAME_TO_FRONTEND_COL_NAME = {
    "zpid": "Property ID",
    "city": "City",
    "purchase_price": "Price",
    "restimate": "Rent Estimate",
    "year_built": "Year Built",
    "home_type": "Home Type",
    "bedrooms": "Bedrooms",
    "bathrooms": "Bathrooms",
    "zip_code": "Zip Code",
    "gross_rent_multiplier": "Gross Rent Multiplier",
    "page_view_count": "Times Viewed",
    "favorite_count": "Favorite Count",
    "days_on_zillow": "Days on Zillow",
    "property_tax_rate": "Tax Rate (%)",
    "living_area": "Living Area (sq ft)",
    "lot_size": "Lot Size (sq ft)",
    "mortgage_rate": "Mortgage Rate (%)",
    "annual_homeowners_insurance": "Annual Insurance",
    "monthly_hoa": "HOA Fee (Monthly)",
    "home_features_score": "Home Features Score",
    "is_waterfront": "Waterfront",
    # Down payment based keys.
    "adj_CoC_5%_down": "Adjusted Cash on Cash (5% Down)",
    "adj_CoC": "Adjusted Cash on Cash",
    "rental_income_5%_down": "Rental Income (5% Down)",
    "rental_income": "Rental Income",
    "Beta_5%_down": "Beta (5% Down)",
    "Beta": "Beta",
    "Alpha_5%_down": "Alpha (5% Down)",
    "Alpha": "Alpha",
    "breakeven_price_5%_down": "Breakeven Price (5% Down)",
    "breakeven_price": "Breakeven Price",
    "is_breaven_price_offending_5%_down": "Is Breakeven Price Offending (5% Down)",
    "is_breaven_price_offending": "Is Breakeven Price Offending",
    "snp_equivalent_price_5%_down": "S&P Competative Price (5% Down)",
    "snp_equivalent_price": "S&P Competative Price",
    "CoC_no_prepaids_5%_down": "Cash on Cash w/o Prepaids (5% Down)",
    "CoC_no_prepaids": "Cash on Cash w/o Prepaids",
    "CoC_5%_down": "Cash on Cash (5% Down)",
    "CoC": "Cash on Cash",
    "adj_CoC_no_prepaids_5%_down": "Adjusted CoC w/o Prepaids (5% Down)",
    "adj_CoC_no_prepaids": "Adjusted CoC w/o Prepaids",
    "cap_rate_5%_down": "Cap Rate (5% Down)",
    "cap_rate": "Cap Rate",
    "adj_cap_rate_5%_down": "Adjusted Cap Rate (5% Down)",
    "adj_cap_rate": "Adjusted Cap Rate",
}

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
    return properties_df

def properties_response_from_properties_df(properties_df, num_properties_per_page=1, page=1):
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

    # Add the zpid as a column.
    properties_df.rename(columns=BACKEND_COL_NAME_TO_FRONTEND_COL_NAME, inplace=True)

    if num_properties_found:
        ordered_cols = TARGET_COLUMNS + [col for col in properties_df.columns if col not in set(TARGET_COLUMNS)]
        ordered_properties_data = properties_df[ordered_cols].to_json(orient="records")
    else:
        ordered_properties_data = '{}'
    return {
        "properties": json.loads(ordered_properties_data),
        "total_properties": num_properties_found,
        "total_pages": total_pages,
    }
    

@app.route('/', methods=['GET'])
def home():
    return render_template('home.html')

@app.route('/explore', methods=['GET', 'POST'])
def search():
    if request.method == 'GET':
        return render_template('explore.html')
    request_data = request.get_json()
    page = int(request_data.get('current_page'))
    num_properties_per_page = int(request_data.get('num_properties_per_page'))
    properties_df = properties_df_from_search_request_data(request_data)

    response_data = properties_response_from_properties_df(properties_df, num_properties_per_page=num_properties_per_page, page=page)
    print("data is: ", page, num_properties_per_page, response_data['total_pages'])
    response_json = json.dumps(response_data)
    return Response(response_json, mimetype='application/json')

@app.route('/search', methods=['GET', 'POST'])
def direct_search():
    if request.method == 'GET':
        return render_template('search.html')
    request_data = request.get_json()
    property_id = int(request_data.get('property_id', 0))
    
    if property_id in BACKEND_PROPERTIES_DF.index:
        properties_df = BACKEND_PROPERTIES_DF.loc[[property_id]].copy()
        response_data = properties_response_from_properties_df(properties_df)
        response_json = json.dumps(response_data)
        return Response(response_json, mimetype='application/json')
    else:
        return jsonify({"error": "Property ID not found"})


if __name__ == '__main__':
    app.run(debug=True)
