from flask import Flask, render_template, request, Response, jsonify
import logging
import os
import json
from collections import OrderedDict

from zillowanalyzer.utility.utility import get_abs_path
from zillowanalyzer.analyzers.preprocessing import load_data, preprocess_dataframe, FilterMethod
from zillowanalyzer.analyzers.iterator import get_property_info_from_property_details


app = Flask(__name__)
app.logger.setLevel(logging.INFO)

combined_df = load_data().round(2)

region_to_zip_codes = {
    'LAKELAND_AREA' : {33859,33863,33831,33813,33846,33884,33812,33880,33839,33811,33877,33807,33885,33566,33838,33840,33803,33563,33564,33882,33883,33888,33804,33802,33815,33801,33806,33851,33881,33844,33823,33850,33805,33565,33845,33810,33809,33868},
    'SOUTH_FLORIDA_AREA' : {33158,33186,33176,33156,33256,33283,33296,33106,33193,33183,33173,33143,33149,33233,33185,33146,33114,33124,33133,33222,33165,33175,33155,33234,33194,33129,33145,33134,33255,33199,33184,33109,33174,33144,33135,33131,33130,33231,33188,33195,33197,33238,33239,33242,33243,33245,33247,33257,33265,33266,33269,33299,33101,33102,33111,33112,33116,33119,33151,33152,33153,33163,33164,33128,33206,33139,33132,33126,33125,33182,33136,33191,33172,33122,33192,33198,33142,33127,33137,33140,33010,33166,33178,33147,33150,33138,33141,33011,33017,33002,33013,33012,33261,33016,33167,33168,33161,33154,33181,33054,33014,33018,33162,33015,33160,33169,33055,33056,33280,33179,33180,33009,33008,33027,33025,33023,33029,33022,33081,33082,33083,33084,33028,33019,33021,33020,33026,33024,33330,33004,33329,33331,33328,33314,33312,33332,33315,33316,33324,33336,33317,33326,33325,33327,33348,33355,33388,33301,33394,33302,33303,33307,33310,33318,33320,33335,33338,33339,33340,33345,33346,33349,33337,33304,33311,33322,33305,33313,33323,33306,33319,33359,33334,33308,33351,33309,33321,33068,33093,33097,33060,33061,33077,33069,33071,33063,33066,33062,33075,33065,33074,33064,33072,33073,33067,33442,33076,33441,33443,33486,33432,33433,33428,33427,33429,33481,33497,33499,33488,33431,33434,33498,33487,33496,33484,33445,33446,33444,33482,33448,33483,33473,33437,33435,33474,33424,33425,33436,33426,33472,33462,33467,33463,33449,33464,33465,33466,33461,33460,33454,33414,33413,33415,33406,33405,33480,33411,33421,33422,33402,33416,33401,33409,33417,33407,33419,33404,33420,33403,33412,33410,33408,33418}
}

@app.route('/', methods=['GET'])
def home():
    return render_template('home.html')

@app.route('/explore', methods=['GET', 'POST'])
def search():
    if request.method == 'GET':
        return render_template('explore.html')
    data = request.get_json()
    region = data.get('region')
    home_type = data.get('home_type')
    year_built = int(data.get('year_built'))
    max_price = float(data.get('max_price'))
    city = data.get('city')
    is_waterfront = bool(data.get('is_waterfront'))
    is_cashflowing = bool(data.get('is_cashflowing'))
    number_deals = int(data.get('num_deals'))

    filtered_df = combined_df.copy()
    if region != "ANY_AREA":
        filtered_df = filtered_df[filtered_df['zip_code'].isin(region_to_zip_codes[region])]
    if home_type != "ANY":
        filtered_df = filtered_df[filtered_df['home_type'] == home_type]
    if year_built:
        filtered_df = filtered_df[filtered_df['year_built'] >= year_built]
    if max_price:
        filtered_df = filtered_df[filtered_df['purchase_price'] <= max_price]
    if is_waterfront:
        filtered_df = filtered_df[filtered_df['is_waterfront'] > 0.0]
    if is_cashflowing:
        filtered_df = filtered_df[filtered_df['adj_CoC 5.0% Down'] >= 0.0]
    if city:
        filtered_df = filtered_df[filtered_df['city'] == city.title()]

    number_properties = filtered_df.shape[0]
    filtered_df = filtered_df.sort_values(by='adj_CoC 5.0% Down', ascending=False)
    filtered_df = filtered_df[:number_deals] if number_deals else filtered_df

    for zpid, filtered_property in filtered_df.iterrows():
        zip_code, zpid = int(filtered_property['zip_code']), int(zpid)
        property_details_path = get_abs_path(f'Data/PropertyDetails/{zip_code}/{zpid}_property_details.json')
        if not os.path.exists(property_details_path):
            filtered_df.drop(zpid, axis=0, inplace=True)
        with open(property_details_path, 'r') as json_file:
            property_details = json.load(json_file)
            # Yield the loaded JSON data
            if 'props' in property_details:
                property_info = get_property_info_from_property_details(property_details)
                image_url = property_info['originalPhotos'][0]['mixedSources']['jpeg'][1]['url']
                filtered_df.loc[zpid, 'image_url'] = image_url
                filtered_df.loc[zpid, 'property_url'] = 'https://zillow.com' + property_info['hdpUrl']
            else:
                filtered_df.loc[zpid, 'image_url'] = None
                filtered_df.loc[zpid, 'property_url'] = None

    # Add the zpid as a column.
    filtered_df = filtered_df.reset_index()
    filtered_df.rename(columns={'index': 'zpid'}, inplace=True)

    if number_properties:
        target_cols = ['image_url', 'zpid', 'city', 'purchase_price', 'breakeven_price 5.0% Down', 'snp_equivalent_price 5.0% Down', 'is_breaven_price_offending 5.0% Down', 'restimate', 'adj_CoC 5.0% Down', 'rental_income 5.0% Down', 'year_built', 'home_type', 'bedrooms', 'bathrooms']
        ordered_cols = target_cols + [col for col in filtered_df.columns if col not in set(target_cols)]
        ordered_data = filtered_df[ordered_cols].to_json(orient="records")
    else:
        ordered_data = '{}'

    response_data = {
        "properties": json.loads(ordered_data),
        "total_properties": number_properties,
    }
    response_json = json.dumps(response_data)
    return Response(response_json, mimetype='application/json')

@app.route('/search', methods=['GET', 'POST'])
def direct_search():
    if request.method == 'GET':
        return render_template('search.html')
    data = request.get_json()
    zpid = int(data.get('zpid', 0))
    
    
    if zpid in combined_df.index:
        property_df = combined_df.loc[[zpid]].copy()
        property_df.reset_index(inplace=True)
        property_df.rename(columns={'index': 'zpid'}, inplace=True)

        zip_code = int(property_df.at[0, 'zip_code'])

        try:
            with open(get_abs_path(f'Data/PropertyDetails/{zip_code}/{zpid}_property_details.json'), 'r') as json_file:
                property_details = json.load(json_file)

            if 'props' in property_details:
                property_info = get_property_info_from_property_details(property_details)
                image_url = property_info['originalPhotos'][0]['mixedSources']['jpeg'][1]['url']
                property_df['image_url'] = image_url
                property_df['property_url'] = 'https://zillow.com' + property_info['hdpUrl']
                property_df['city'] = property_info['city']
            else:
                property_df['image_url'] = None
                property_df['property_url'] = None
                property_df['city'] = None
            
        except FileNotFoundError:
            print(f"File not found")

        target_cols = ['image_url', 'zpid', 'city', 'purchase_price', 'restimate', 'adj_CoC 5.0% Down', 'rental_income 5.0% Down', 'year_built', 'home_type', 'bedrooms', 'bathrooms']
        ordered_cols = target_cols + [col for col in property_df.columns if col not in set(target_cols)]
        ordered_data = property_df[ordered_cols].to_json(orient="records")

        response_data = {
            "properties": json.loads(ordered_data)
        }
        response_json = json.dumps(response_data)
        return Response(response_json, mimetype='application/json')
    else:
        return jsonify({"error": "ZPID not found"})


if __name__ == '__main__':
    app.run(debug=True)
