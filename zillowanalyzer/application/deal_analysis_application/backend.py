import os
import logging
import json

from backend_util import BACKEND_PROPERTIES_DF, properties_df_from_search_request_data, properties_response_from_properties_df

from flask import Flask, render_template, request, Response, jsonify, render_template, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature


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
        'name': ('Test', 'Name'),
        'password': generate_password_hash('pass'),
        'is_professional': True,
        'confirmed': True,
        'saved': {2054668176, 125785286, 2054529325},
    },
    'unverified@gmail.com': {
        'id': 2,
        'name': ('Unverified', 'Name'),
        'password': generate_password_hash('pass'),
        'is_professional': False,
        'confirmed': False,
        'saved': {},
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
        self.first_name, self.last_name = users[email]['name']
        self.confirmed = users[email]['confirmed']
        self.saved = users[email]['saved']

    def get_id(self):
        return str(self.id)

@login_manager.user_loader
def load_user(user_id):
    for email, user in users.items():
        if user['id'] == int(user_id):
            return User(email)
    return None

'''
A message based flash message with the following funcitonality:
- Regular flash functionality, i.e. message and message category.
- Multiple flash messages per html via area.
- Custom animation types.
'''
def flash_message(message, category='info', area='default', animation=None, **kwargs):
    return jsonify({
        **kwargs,
        'fancy_flash_messages': [{
            'message': message,
            'category': category,
            'area': area,
            'animation': animation
        }]
    })


########################
## EMAIL VERIFICATION ##
########################

def generate_token(data, salt='generic-salt', expiration=3600):
    """ Generate a secure token for a given data with a salt and expiration time. """
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    return serializer.dumps(data, salt=salt)

def verify_token(token, salt='generic-salt', expiration=3600):
    """ Verify a token and return the data if valid; otherwise, return None. """
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    try:
        data = serializer.loads(token, salt=salt, max_age=expiration)
        return data
    except (SignatureExpired, BadSignature):
        return None

def send_confirmation_email(user_email):
    token = generate_token(user_email, salt='email-confirm-salt', expiration=3600)
    activation_url = url_for('confirm_email', token=token, _external=True)
    html_body = render_template('confirm_email/email-activate.html', activation_url=activation_url, user_email=user_email)
    text_body = render_template('confirm_email/email-activate.txt', activation_url=activation_url, user_email=user_email)
    subject = "Confirm Your Email"
    message = Message(subject, sender=app.config['MAIL_USERNAME'], recipients=[user_email])
    message.body = text_body
    message.html = html_body
    mail.send(message)

def send_reset_email(user_email):
    token = generate_token(user_email, salt='password-reset-salt', expiration=1800)
    reset_url = url_for('set_new_password', token=token, _external=True)
    html_body = render_template('reset_password/email-reset.html', reset_url=reset_url, user_email=user_email)
    text_body = render_template('reset_password/email-reset.txt', reset_url=reset_url, user_email=user_email)
    subject = "Set New Password"
    message = Message(subject, sender=app.config['MAIL_USERNAME'], recipients=[user_email])
    message.body = text_body
    message.html = html_body
    mail.send(message)

def properties_response(properties_df, num_properties_per_page=1, page=1):
    saved_zpids = current_user.saved if current_user.is_authenticated else {}
    response_data = properties_response_from_properties_df(properties_df, num_properties_per_page=num_properties_per_page, page=page, saved_zpids=saved_zpids)
    # If the user is logged out, add a description to log in.
    if current_user.is_authenticated and not current_user.is_anonymous:
        response_data["descriptions"]["Save"] = "Save/unsave this property to go back to it later."
    else:
        response_data["descriptions"]["Save"] = "To save a property you must first login."
    return response_data


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

    response_data = properties_response(properties_df, num_properties_per_page=num_properties_per_page, page=page)
    response_json = json.dumps(response_data)
    return Response(response_json, mimetype='application/json')

@app.route('/search', methods=['GET', 'POST'])
def direct_search():
    if request.method == 'GET':
        return render_template('search.html')
    request_data = request.get_json()
    page = int(request_data.get('current_page'))
    property_address = request_data.get('property_address', '')
    properties_df = search_properties(property_address)
    
    if not properties_df.empty:
        response_data = properties_response(properties_df, num_properties_per_page=min(len(properties_df), 10), page=page)
        response_json = json.dumps(response_data)
        return Response(response_json, mimetype='application/json')
    else:
        return jsonify({"error": "Property ID not found"})

@app.route('/saved', methods=['GET', 'POST'])
@login_required
def saved():
    if request.method == 'GET':
        return render_template('saved.html')
    request_data = request.get_json()
    page = int(request_data.get('current_page'))
    properties_df = BACKEND_PROPERTIES_DF.loc[list(current_user.saved)]
    
    if not properties_df.empty:
        response_data = properties_response(properties_df, num_properties_per_page=min(len(properties_df), 10), page=page)
        response_json = json.dumps(response_data)
        return Response(response_json, mimetype='application/json')
    else:
        return jsonify({"error": "Property ID not found"})

@app.route('/toggle-save', methods=['POST'])
@login_required
def toggle_save():
    data = request.get_json()
    property_id = data.get('propertyId')
    if property_id in current_user.saved:
        current_user.saved.remove(property_id)
        saved = False
    else:
        current_user.saved.add(property_id)
        saved = True
    return jsonify({'success': True, 'saved': saved})

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
        if current_user.is_authenticated and not current_user.is_anonymous:
            return redirect(url_for('profile'))
        return render_template('login.html')
    data = request.get_json()
    user_email = data.get('user_email')
    user_password = data.get('user_password')

    user = users.get(user_email)
    if not user or not check_password_hash(user['password'], user_password):
        response = flash_message('Invalid username or password.', category='warning', area='login', animation='shake')
        return response, 401
    elif not user['confirmed']:
        response = flash_message('Please verify your email.', category='warning', area='login', animation='shake')
        return response, 401

    user_obj = User(user_email)
    login_user(user_obj)
    response = flash_message('Login successful.', category='success', area='login', animation='fadeIn', redirect=url_for('profile'))
    return response, 200


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
        first_name = data.get('firstName')
        last_name = data.get('lastName')
        user_email = data.get('userEmail')
        user_password = data.get('userPassword')
        is_professional = data.get('isProfessional')

        # Check if email already exists
        if user_email in users:
            response = flash_message('Email already registered.', category='warning', area='register', animation='shake')
            return response, 409

        # Add user to the "database"
        users[user_email] = {
            'id': len(users) + 1,
            'name': (first_name, last_name),
            'password': generate_password_hash(user_password),
            'is_professional': is_professional,
            'confirmed': False,
            'saved': {}
        }

        # Send confirmation email
        send_confirmation_email(user_email)
        response = flash_message('Please confirm your email address.', category='success', area='register', animation='fadeIn', redirect=url_for('login'))
        return response, 201

@app.route('/confirm/<token>')
def confirm_email(token):
    try:
        email = verify_token(token, salt='email-confirm-salt', expiration=3600)
    except Exception as e:
        flash('The confirmation link is invalid or has expired.', 'error')
        return render_template(url_for('login'), message='The confirmation link is invalid or has expired.')

    user = users.get(email)
    if not user:
        flash('Invalid or unknown email.', 'error')
        return render_template(url_for('login'), message='Invalid or unknown email.')

    if user['confirmed']:
        flash('Your account has already been confirmed.', 'info')
        return render_template('confirm_email/confirm-email.html', message='Your account has already been confirmed.')

    users[email]['confirmed'] = True
    flash('Thank you! Your account has been confirmed.', 'success')
    return render_template('confirm_email/confirm-email.html', message='Your account has been confirmed. Thank you!')

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if request.method == 'GET':
        # Pre-fill the email if the user is authenticated.
        email = current_user.email if current_user.is_authenticated else None
        return render_template('reset_password/reset-password.html', email=email, current_user=current_user)

    elif request.method == 'POST':
        user_email = current_user.email if current_user.is_authenticated else request.json['email']
        user = users.get(user_email)
        if user:
            response = flash_message('A password reset link has been sent to your email.', category='success', area='reset', animation='fadeIn', redirect=url_for('login'))
            send_reset_email(user_email)
            return response, 200
        else:
            response = flash_message('No account found with that email address.', category='warning', area='reset', animation='shake')
            return response, 409

@app.route('/set-new-password/<token>', methods=['GET', 'POST'])
def set_new_password(token):
    if request.method == 'GET':
        try:
            user_email = verify_token(token, salt='password-reset-salt', expiration=1800)
            if user_email in users:
                return render_template('reset_password/set-new-password.html', token=token, email=user_email)
            else:
                flash('No user associated with this email.', 'error')
                return redirect(url_for('reset_password'))
        except (SignatureExpired, BadSignature):
            flash('The reset link is invalid or has expired.', 'error')
            return redirect(url_for('reset_password'))
    elif request.method == 'POST':
        try:
            user_email = verify_token(token, salt='password-reset-salt', expiration=1800)
            new_password = request.json['new_password']
            user = users.get(user_email)
            user['password'] = generate_password_hash(new_password)
            return flash_message('Your password has been updated.', category='success', area='set-new-password', animation='fadeIn'), 200
        except (SignatureExpired, BadSignature):
            return flash_message('The reset link is invalid or has expired.', category='error', area='set-new-password', animation='shake'), 400
        except KeyError:
            return flash_message('Invalid data received.', category='error', area='set-new-password', animation='shake'), 400

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    if request.method == 'GET':
        return render_template('profile.html')
    return 'Welcome to your Profile, {}'.format(current_user.email)

if __name__ == '__main__':
    app.run(debug=True)
