from flask import Flask, request, render_template, jsonify
import requests
import os
import sqlite3
import logging
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
from datetime import datetime, timezone
import json
import uuid
import aiohttp
import asyncio
from contextlib import contextmanager
import tempfile
import os.path

app = Flask(__name__)
load_dotenv()

# Configuration
APP_ID = os.getenv('FB_APP_ID')
APP_SECRET = os.getenv('FB_APP_SECRET')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB limit

# Ensure logs folder exists
os.makedirs('logs', exist_ok=True)

logging.basicConfig(
    filename='logs/app.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

@contextmanager
def get_db_connection():
    conn = sqlite3.connect('tokens.db')
    try:
        yield conn
    finally:
        conn.close()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def validate_page_token(page_id, access_token):
    try:
        response = requests.get(
            f"https://graph.facebook.com/{page_id}",
            params={'access_token': access_token, 'fields': 'id,name,is_published'},
            timeout=5
        )
        response.raise_for_status()
        data = response.json()
        return data.get('is_published', True)
    except requests.RequestException as e:
        logging.error(f"Token validation failed for page {page_id}: {str(e)}")
        return False

def init_db():
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tokens'")
            if not c.fetchone():
                c.execute('''CREATE TABLE tokens (
                    page_id TEXT,
                    access_token TEXT,
                    page_name TEXT,
                    account_id TEXT,
                    created_at TEXT,
                    is_valid INTEGER,
                    PRIMARY KEY (page_id, account_id)
                )''')
            else:
                c.execute("PRAGMA table_info(tokens)")
                if 'is_valid' not in [col[1] for col in c.fetchall()]:
                    c.execute("ALTER TABLE tokens ADD COLUMN is_valid INTEGER DEFAULT 1")
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"DB init failed: {e}")
        raise

init_db()

@app.route('/')
def index():
    return render_template('index.html', app_id=APP_ID)

@app.route('/get_pages', methods=['POST'])
async def get_pages():
    try:
        user_access_token = request.json.get('access_token')
        if not user_access_token:
            return jsonify({'error': 'Access token required'}), 400

        async def fetch_all_pages(token):
            pages_data = []
            url = 'https://graph.facebook.com/me/accounts'
            params = {'access_token': token, 'limit': 100}
            async with aiohttp.ClientSession() as session:
                while url:
                    async with session.get(url, params=params) as response:
                        response.raise_for_status()
                        data = await response.json()
                        pages_data.extend(data['data'])
                        url = data.get('paging', {}).get('next')
                        params = {}
            return pages_data

        pages_data = await fetch_all_pages(user_access_token)
        all_pages = []

        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute('DELETE FROM tokens WHERE account_id = ?', (user_access_token[:10],))
            for page in pages_data:
                is_valid = validate_page_token(page['id'], page['access_token'])
                c.execute('INSERT OR REPLACE INTO tokens VALUES (?, ?, ?, ?, ?, ?)', (
                    page['id'], page['access_token'], page['name'],
                    user_access_token[:10],
                    datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
                    1 if is_valid else 0
                ))
                all_pages.append({
                    'id': page['id'],
                    'access_token': page['access_token'],
                    'name': page['name'],
                    'account_id': user_access_token[:10],
                    'is_valid': is_valid
                })
            conn.commit()

        return jsonify({'pages': all_pages})
    except Exception as e:
        logging.error(f"Get pages failed: {str(e)}")
        return jsonify({'error': f'Server error: {str(e)}'}), 500

@app.route('/post_to_pages', methods=['POST'])
async def post_to_pages():
    try:
        content = request.form.get('content')
        image_file = request.files.get('image_file')
        pages = request.form.get('pages')

        if not content or not pages:
            return jsonify({'error': 'Content and pages required'}), 400

        try:
            pages = json.loads(pages)
        except json.JSONDecodeError:
            return jsonify({'error': 'Invalid pages data'}), 400

        file_path = None
        filename = None
        ext = None
        if image_file and allowed_file(image_file.filename):
            if image_file.content_length > MAX_FILE_SIZE:
                return jsonify({'error': f'File size exceeds {MAX_FILE_SIZE // 1024 // 1024}MB'}), 400
            ext = image_file.filename.rsplit('.', 1)[1].lower()
            filename = f"{uuid.uuid4()}.{ext}"
            with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{ext}') as temp_file:
                file_path = temp_file.name
                image_file.save(file_path)

        async def post_to_page(session, page, content, file_path=None, filename=None, ext=None):
            if not page.get('is_valid'):
                return f"Skipped {page['name']}: Invalid token"

            try:
                if file_path:
                    endpoint = f"https://graph.facebook.com/{page['id']}/photos"
                    form_data = aiohttp.FormData()
                    form_data.add_field('message', content)
                    form_data.add_field('access_token', page['access_token'])
                    with open(file_path, 'rb') as f:
                        form_data.add_field('source', f, filename=filename, content_type=f'image/{ext}')
                        async with session.post(endpoint, data=form_data) as response:
                            response.raise_for_status()
                            return f"Posted to {page['name']} with image"
                else:
                    endpoint = f"https://graph.facebook.com/{page['id']}/feed"
                    data = {'message': content, 'access_token': page['access_token']}
                    async with session.post(endpoint, data=data) as response:
                        response.raise_for_status()
                        return f"Posted to {page['name']} (text only)"
            except aiohttp.ClientError as e:
                return f"Failed to post to {page['name']}: {str(e)}"
            finally:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)

        results = []
        async with aiohttp.ClientSession() as session:
            tasks = [post_to_page(session, page, content, file_path, filename, ext) for page in pages]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            results = [str(r) if not isinstance(r, str) else r for r in results]

        return jsonify({'message': '\n'.join(results)})
    except Exception as e:
        logging.error(f"Post error: {str(e)}")
        return jsonify({'error': f'Server error: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5000)
