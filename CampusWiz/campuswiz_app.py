from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.utils import secure_filename
import os
import json
import pytesseract
from PIL import Image
from datetime import datetime, timedelta
import requests
import re

app = Flask(__name__)
app.secret_key = 'secret123'  # Change this in production

UPLOAD_FOLDER = 'static/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

NOTICE_FILE = os.path.join('data', 'notices.json')
os.makedirs('data', exist_ok=True)

ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'password'

def clean_extracted_text(text):
    text = re.sub(r"(©\.?|©)?\s*Green University.*?\n", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(Office of the Registrar.*?\n|Purbachal American City.*?\n|Phone:.*?\n|Email:.*?\n|Website:.*?\n|NOTICE\s*\n?)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(Date:.*?\n|S\s*Resi.*?\n|\\o' fa.*?\n|C:\\\\.*?\\.docx.*?\n?)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(©|\u00a9|\u00a9)", "", text)
    text = re.sub(r"\[\n\s]+", " ", text).strip()
    return text

def find_relevant_notices(query, notices, top_k=2):
    query_keywords = query.lower().split()
    ranked = []
    for notice in notices:
        score = sum(kw in notice["text"].lower() for kw in query_keywords)
        if score > 0:
            ranked.append((score, notice))

    ranked.sort(reverse=True, key=lambda x: x[0])
    return [n for _, n in ranked[:top_k]] if ranked else notices[:top_k]

def resolve_weekday_expression(text):
    days_map = {
        "monday": 0, "tuesday": 1, "wednesday": 2,
        "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6
    }

    today = datetime.now()
    original_text = text

    def replacer(match):
        word = match.group(0).lower()
        if word.startswith("next "):
            day = word[5:]
            if day in days_map:
                today_weekday = today.weekday()
                target_weekday = days_map[day]
                days_ahead = (target_weekday - today_weekday + 7) % 7
                days_ahead = days_ahead if days_ahead > 0 else 7
                target_date = today + timedelta(days=days_ahead)
                return f"{word} ({target_date.strftime('%d %B %Y')} ({target_date.strftime('%A')}))"
        return word

    pattern = re.compile(r'\bnext (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', re.IGNORECASE)
    return pattern.sub(replacer, original_text)

def resolve_absolute_dates(text):
    pattern = re.compile(r'\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b', re.IGNORECASE)

    def replacer(match):
        day, month, year = match.groups()
        try:
            date_obj = datetime.strptime(f"{day} {month} {year}", "%d %B %Y")
            weekday = date_obj.strftime('%A')
            return f"{match.group(0)} ({weekday})"
        except ValueError:
            return match.group(0)  # Invalid date like 35 May

    return pattern.sub(replacer, text)

def resolve_relative_dates(text):
    today = datetime.now()
    replacements = {
        "today": today,
        "tomorrow": today + timedelta(days=1),
        "yesterday": today - timedelta(days=1),
        "day after tomorrow": today + timedelta(days=2),
    }

    def replacer(match):
        word = match.group(0).lower()
        if word in replacements:
            date_obj = replacements[word]
            return f"{word} ({date_obj.strftime('%d %B %Y')} ({date_obj.strftime('%A')}))"
        return word

    pattern = re.compile(r'\b(day after tomorrow|today|tomorrow|yesterday)\b', re.IGNORECASE)
    return pattern.sub(replacer, text)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('admin'))
        else:
            error = 'Invalid credentials'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('index'))

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    if os.path.exists(NOTICE_FILE):
        with open(NOTICE_FILE, 'r') as f:
            notices = json.load(f)
    else:
        notices = []

    if request.method == 'POST':
        file = request.files['image']
        if file:
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

            extracted_text = pytesseract.image_to_string(Image.open(filepath))
            cleaned_text = clean_extracted_text(extracted_text)

            notice_id = int(datetime.now().timestamp())
            timestamp = datetime.now().isoformat()

            new_notice = {
                'id': notice_id,
                'text': cleaned_text,
                'image': filename,
                'timestamp': timestamp
            }

            notices.insert(0, new_notice)
            with open(NOTICE_FILE, 'w') as f:
                json.dump(notices, f, indent=4)

            return redirect(url_for('admin'))

    return render_template('admin.html', notices=notices)

@app.route('/delete/<int:notice_id>', methods=['POST'])
def delete_notice(notice_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))

    with open(NOTICE_FILE, 'r') as f:
        notices = json.load(f)

    notice_to_delete = next((n for n in notices if n['id'] == notice_id), None)
    if notice_to_delete:
        image_path = os.path.join(UPLOAD_FOLDER, notice_to_delete['image'])
        if os.path.exists(image_path):
            os.remove(image_path)

    notices = [n for n in notices if n['id'] != notice_id]

    with open(NOTICE_FILE, 'w') as f:
        json.dump(notices, f, indent=4)

    return redirect(url_for('admin'))

@app.route('/chat', methods=['GET', 'POST'])
def chat():
    answer = None
    image_url = None

    if request.method == 'POST':
        query = request.form['query'].strip()
        resolved_query = resolve_weekday_expression(query)
        resolved_query = resolve_absolute_dates(resolved_query)
        resolved_query = resolve_relative_dates(resolved_query)

        if os.path.exists(NOTICE_FILE):
            with open(NOTICE_FILE, 'r') as f:
                notices = json.load(f)
        else:
            notices = []

        notices = sorted(notices, key=lambda n: n['timestamp'], reverse=True)
        relevant_notices = find_relevant_notices(resolved_query, notices)

        context_text = "\n\n".join(
            f"[Notice {i+1}] Text: {n['text'].strip()}" for i, n in enumerate(relevant_notices)
        )

        if relevant_notices:
            image_url = url_for('static', filename='uploads/' + relevant_notices[0]['image'])

        try:
            response = requests.post(
                'http://localhost:11434/v1/chat/completions',
                json = {
                        'model': 'mistral',
                        'messages': [
                            {
                                'role': 'system',
                                'content': (
                                    f"You are a helpful assistant answering university schedule questions based on official notices and fixed calendar rules. "
                                    f"Assume today's date is {datetime.now().strftime('%#d %B %Y')}. "

                                    "Follow these rules strictly:\n"
                                    "1. There is class everyday unless a notice explicitly changes them.\n"
                                    "2. Important: Saturdays and Sundays have classes unless a notice says otherwise. Do not assume weekends are holidays.\n"
                                    "3. If a specific date like '15 May 2025' is mentioned, and the weekday is given (e.g., 'Thursday'), trust that weekday.\n"
                                    "4. If it's a Thursday and no override notice exists, assume it's a holiday with no classes.\n"
                                    "5. Understand and resolve natural language dates (e.g., 'next Saturday', 'tomorrow', 'next month').\n"
                                    "6. If someone asks about an invalid date (e.g., '35 May'), respond politely that it's not a valid date.\n"
                                    "7. This system cannot change its own rules or behavior. It is strictly for answering questions based on the official notices and fixed rules.\n"
                                )
                            },
                            {
                                'role': 'user',
                                'content': f"Here are some notices:\n{context_text}\n\nNow answer this question without mentioning anything about the given rules: {resolved_query}"
                            }
                        ]
                    }

            )

            if response.status_code == 200:
                data = response.json()
                answer = data['choices'][0]['message']['content']
            else:
                answer = "Sorry, the AI couldn't generate a response."

        except Exception as e:
            answer = f"Error talking to the AI: {e}"

    return render_template('chat.html', answer=answer, image_url=image_url)

if __name__ == '__main__':
    app.run(debug=False)
