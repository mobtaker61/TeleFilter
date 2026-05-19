"""
پنل مدیریت TeleFilter
اجرا کن: python panel.py
سپس مرورگر را باز کن: http://127.0.0.1:5000
"""
import json
import webbrowser
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)
CONFIG_PATH = 'config.json'


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"api_id": "", "api_hash": "", "target_group_id": "", "topics": []}


def save_config(data: dict):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/config', methods=['GET'])
def get_config():
    return jsonify(load_config())


@app.route('/api/config', methods=['POST'])
def update_config():
    data = request.get_json()
    save_config(data)
    return jsonify({'ok': True})


if __name__ == '__main__':
    webbrowser.open('http://127.0.0.1:5000')
    app.run(debug=False, port=5000)
