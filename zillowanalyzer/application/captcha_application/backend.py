from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/')
def index():
    return app.send_static_file('captcha_error.html')

@app.route('/save-path', methods=['POST'])
def save_path():
    data = request.json
    # Assuming `data` contains the mouse path
    with open('mouse_paths.txt', 'a') as file:
        file.write(f"{data}\n")
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(debug=True)
