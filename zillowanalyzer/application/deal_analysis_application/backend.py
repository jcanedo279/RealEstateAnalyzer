import os
import logging
import json

from backend_util import BACKEND_PROPERTIES_DF, properties_df_from_search_request_data, properties_response_from_properties_df

from flask import Flask, render_template, request, Response, jsonify, render_template, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer


app = Flask(__name__)
app.logger.setLevel(logging.INFO)
app.secret_key = os.environ.get('APP_SECRET_KEY')

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

app.config['MAIL_SERVER'] = os.environ.get('MAIN_SERVER')
app.config['MAIL_PORT'] = os.environ.get('MAIL_PORT')
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_APP_PASSWORD')
mail = Mail(app)


# Assuming a very simple user store
users = {
    'test@gmail.com': {
        'id': 1,
        'password': generate_password_hash('password'),
        'confirmed': False
    }
}

def search_properties(query_address):
    # Filter the DataFrame for addresses that contain the query string, case-insensitive
    return BACKEND_PROPERTIES_DF[BACKEND_PROPERTIES_DF['street_address'].str.contains(query_address, case=False, na=False)]


#####################
## USER MANAGEMENT ##
#####################

class User(UserMixin):
    def __init__(self, email):
        self.email = email
        self.id = users[email]['id']
        self.confirmed = users[email]['confirmed']

    def get_id(self):
        return str(self.id)

@login_manager.user_loader
def load_user(user_id):
    for email, user in users.items():
        if user['id'] == int(user_id):
            return User(email)
    return None


########################
## EMAIL VERIFICATION ##
########################

def generate_confirmation_token(email):
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    return serializer.dumps(email, salt='email-confirm-salt')

def send_confirmation_email(user_email):
    token = generate_confirmation_token(user_email)
    activation_url = url_for('confirm_email', token=token, _external=True)
    html_body = render_template('email-activate.html', activation_url=activation_url, user_email=user_email)
    text_body = render_template('email-activate.txt', activation_url=activation_url, user_email=user_email)
    subject = "Please confirm your email"
    message = Message(subject, sender=app.config['MAIL_USERNAME'], recipients=[user_email])
    message.body = text_body
    message.html = html_body
    mail.send(message)


################
## APP ROUTES ##
################

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
    response_json = json.dumps(response_data)
    return Response(response_json, mimetype='application/json')

@app.route('/search', methods=['GET', 'POST'])
def direct_search():
    if request.method == 'GET':
        return render_template('search.html')
    request_data = request.get_json()
    page = int(request_data.get('current_page'))
    property_address = request_data.get('property_address', '')
    property_df = search_properties(property_address)
    # The Sanctuary at Babcock Ranch
    
    if not property_df.empty:
        response_data = properties_response_from_properties_df(property_df, num_properties_per_page=min(len(property_df), 10), page=page)
        response_json = json.dumps(response_data)
        return Response(response_json, mimetype='application/json')
    else:
        return jsonify({"error": "Property ID not found"})

@app.route('/report', methods=['POST'])
def report():
    request_data = request.get_json()
    user_email = request_data.get('user_email', '')
    issue_description = request_data.get('issue_description', '')

    app.logger.info(f"{user_email} has filed an issue: {issue_description}.")
    
    return jsonify({"success": "Issue reported successfully"}), 200

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        return render_template('login.html')
    data = request.get_json()  # Use get_json() to extract JSON data from the request
    user_email = data.get('user_email')
    user_password = data.get('user_password')
    user = users.get(user_email)

    app.logger.info(f"Login attempt with email: '{user_email}' and password: '{user_password}'")
    if not user['confirmed']:
        flash('Please verify your email.')
    elif user_email and check_password_hash(user['password'], user_password):
        user_obj = User(user_email)
        login_user(user_obj)
        app.logger.info("correct")
        return redirect(url_for('profile'))
    else:
        flash('Invalid username or password.')

    return jsonify({"success": "Logged in successfully"}), 200

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template('register.html')
    elif request.method == 'POST':
        data = request.get_json()
        user_email = data.get('user_email')
        user_password = data.get('user_password')

        # Check if email already exists
        if user_email in users:
            return jsonify({"message": "Email already registered."}), 409

        # Add user to the "database"
        users[user_email] = {
            'id': len(users) + 1,
            'password': generate_password_hash(user_password),
            'confirmed': False
        }

        # Send confirmation email
        send_confirmation_email(user_email)
        return jsonify({"message": "Please confirm your email address."}), 201

@app.route('/confirm/<token>')
def confirm_email(token):
    try:
        email = URLSafeTimedSerializer(app.config['SECRET_KEY']).loads(token, salt='email-confirm-salt', max_age=3600)
    except Exception as e:
        flash('The confirmation link is invalid or has expired.', 'error')
        return render_template('error.html', message='The confirmation link is invalid or has expired.')

    user = users.get(email)
    if not user:
        flash('Invalid or unknown email.', 'error')
        return render_template('error.html', message='Invalid or unknown email.')

    if user['confirmed']:
        flash('Your account has already been confirmed.', 'info')
        return render_template('confirmation.html', message='Your account has already been confirmed.')

    users[email]['confirmed'] = True
    flash('Thank you! Your account has been confirmed.', 'success')
    return render_template('confirmation.html', message='Your account has been confirmed. Thank you!')

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'GET':
        return render_template('profile.html')
    return 'Welcome to your Profile, {}'.format(current_user.email)


if __name__ == '__main__':
    app.run(debug=True)
