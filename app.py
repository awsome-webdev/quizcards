from flask import Flask, request, jsonify, render_template, redirect, url_for
import json
import os
import re
import uuid
import requests
import fitz  # PyMuPDF
import base64
from flask_login import (
    LoginManager, UserMixin,
    login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
try:
    from openrouter import OpenRouter
except ImportError:
    OpenRouter = None

app = Flask(__name__)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
app.secret_key = 'idkwhattochooseforsessionkey1234567890'
root = os.getcwd() + '/'
def parse_generic_quizlet_pdf(pdf_stream):
    doc = fitz.open(stream=pdf_stream.read(), filetype="pdf")
    
    # 1. Extract all text and images
    full_text = ""
    extracted_images = []
    
    for page in doc:
        full_text += page.get_text() + "\n"
        # Extract images and filter tiny icons
        for img in page.get_images(full=True):
            xref = img[0]
            base_image = doc.extract_image(xref)
            if len(base_image["image"]) > 2000: # Filter out icons/bullets
                b64 = base64.b64encode(base_image["image"]).decode('utf-8')
                extracted_images.append(f"data:image/{base_image['ext']};base64,{b64}")

    # 2. Split text by Quizlet's numbering pattern (e.g., "1. ", "2. ")
    # This regex looks for a digit followed by a dot at the start of a line
    raw_cards = re.split(r'\n\d+\.\s+', full_text)
    
    # The first element is usually header info (Title, etc.), so we save it
    title_info = raw_cards[0].split('\n')[0] if raw_cards else "Imported Set"
    card_data = raw_cards[1:] # The actual cards

    final_content = []
    
    for i, content in enumerate(card_data):
        # Quizlet usually puts the 'Question' on the first line 
        # and the 'Answer' on the subsequent lines.
        lines = [l.strip() for l in content.split('\n') if l.strip()]
        
        if not lines:
            continue
            
        question = lines[0]
        # Join remaining lines with <br> for the answer
        answer = " <br> ".join(lines[1:]) if len(lines) > 1 else "No answer provided"

        # Assign image based on sequence if available
        img_url = extracted_images[i] if i < len(extracted_images) else None

        final_content.append({
            "question": question,
            "answer": answer,
            "image": img_url
        })

    return title_info, final_content

def parse_entomology_pdf(pdf_stream):
    # Open the PDF from the file stream
    doc = fitz.open(stream=pdf_stream.read(), filetype="pdf")
    
    cards = []
    extracted_images = []
    
    # 1. Extract Text and build logical cards
    # We use a similar logic to before: finding "Order:" implies the previous line was the name.
    full_text = ""
    for page in doc:
        full_text += page.get_text() + "\n"
    
    lines = full_text.split('\n')
    current_card = {}
    potential_name_buffer = None
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        if line.startswith("Order:"):
            # Start a new card using the buffer as the name
            current_card = {
                "question": potential_name_buffer if potential_name_buffer else "Unknown Insect",
                "attributes": [],
                "image": None 
            }
            current_card["attributes"].append(line)
        elif line.startswith("Metamorphosis:"):
            if "attributes" in current_card:
                current_card["attributes"].append(line)
        elif line.startswith("Mouth Parts:"):
            if "attributes" in current_card:
                current_card["attributes"].append(line)
                
                # Finalize the card
                clean_question = re.sub(r'^\d+\.\s*', '', current_card['question'])
                formatted_answer = " <br> ".join(current_card["attributes"])
                
                cards.append({
                    "question": clean_question,
                    "answer": formatted_answer,
                    "image": None  # Will be filled later
                })
                current_card = {}
                potential_name_buffer = None
        else:
            # Buffer potential name (filtering out page numbers/short artifacts)
            if not re.match(r'^\d+ / \d+$', line) and len(line) > 2:
                potential_name_buffer = line

    # 2. Extract Images from all pages
    for page_index in range(len(doc)):
        page = doc[page_index]
        image_list = page.get_images(full=True)
        
        for img_index, img in enumerate(image_list):
            xref = img[0]
            base_image = doc.extract_image(xref)
            image_bytes = base_image["image"]
            
            # Filter out small images (logos/icons) based on size
            if len(image_bytes) < 2000: # Threshold in bytes
                continue
                
            # Convert to Base64 Data URL
            b64_string = base64.b64encode(image_bytes).decode('utf-8')
            mime_type = base_image["ext"]
            data_url = f"data:image/{mime_type};base64,{b64_string}"
            
            extracted_images.append(data_url)

    # 3. Merge Images into Cards
    # We assume sequential ordering (Image 1 matches Insect 1)
    # This is standard for flashcard PDFs
    for i in range(min(len(cards), len(extracted_images))):
        cards[i]["image"] = extracted_images[i]

    return cards

def parse_hybrid_quizlet_pdf(pdf_stream):
    doc = fitz.open(stream=pdf_stream.read(), filetype="pdf")
    
    extracted_images = []
    text_blocks = []

    # 1. Extract images (keeping your original logic)
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            base_image = doc.extract_image(xref)
            if len(base_image["image"]) > 2000:
                b64 = base64.b64encode(base_image["image"]).decode('utf-8')
                extracted_images.append(f"data:image/{base_image['ext']};base64,{b64}")

    # 2. Extract and Filter Text
    full_text = ""
    for page in doc:
        full_text += page.get_text() + "\n"

    raw_segments = re.split(r'\n\d+\.\s+', full_text)
    # Extract global title
    title = raw_segments[0].split('\n')[0].strip() if raw_segments else "Imported Set"
    title = title.replace('&', 'and').replace('?', '')
    
    for segment in raw_segments[1:]:
        # Split into lines and strip whitespace
        lines = [l.strip() for l in segment.split('\n') if l.strip()]
        
        # --- NEW FILTERING LOGIC ---
        cleaned_lines = []
        for line in lines:
            # 1. Remove Card Counts (e.g., "5 / 28")
            if re.match(r'^\d+\s*/\s*\d+$', line):
                continue
            # 2. Remove Quizlet URLs and "Study online at"
            if "quizlet.com" in line.lower() or "study online at" in line.lower():
                continue
            # 3. Remove the Set Title if it repeats on every card
            if line.lower() == title.lower():
                continue
            
            cleaned_lines.append(line)
        # ---------------------------

        if cleaned_lines:
            text_blocks.append(cleaned_lines)

    # 3. Pair them up
    final_cards = []
    
    if len(extracted_images) >= len(text_blocks) and len(text_blocks) > 0:
        for i in range(min(len(text_blocks), len(extracted_images))):
            final_cards.append({
                "question": "", 
                "image": extracted_images[i],
                "answer": " <br> ".join(text_blocks[i]) # Cleaned text here
            })
            
    else:
        for lines in text_blocks:
            # lines[0] is now the "Cabbage Looper" instead of the junk
            answer = " <br> ".join(lines[1:]) if len(lines) > 1 else "No definition"
            final_cards.append({
                "question": lines[0],
                "image": None,
                "answer": answer
            })

    return title, final_cards
class User(UserMixin):
    def __init__(self, id, username, password_hash):
        self.id = id
        self.username = username
        self.password_hash = password_hash

def readjson(file):
    path = f'{root}{file}'
    if os.path.exists(path):
        with open(path, 'r') as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                # File is empty or invalid, treat as empty list
                data = []
        return data
    return []

def load_users():
    return readjson("users.json")

@login_manager.user_loader
def load_user(user_id):
    users = load_users()
    for u in users:
        if str(u["id"]) == str(user_id):
            return User(u["id"], u["username"], u["password"])
    return None

# API Keys Setup
ai_key = ""
search_key = ""
if os.path.exists('keys.json'):
    with open('keys.json') as f:
        keys = json.load(f)
        ai_key = keys[0]['hcai']
        search_key = keys[0]['hcsearch']

# Initialize AI Client if module exists
client = None
if OpenRouter:
    client = OpenRouter(
        api_key=ai_key,
        server_url="https://ai.hackclub.com/proxy/v1",
    )

def ask(prompt):
    if not client: return "AI Client not initialized"
    response = client.chat.send(
        model="qwen/qwen3-32b",
        messages=[
            {"role": "user", "content": prompt}
        ],
        stream=False,
    )
    return response.choices[0].message.content

def search(query, type="web"):
    headers = {"Authorization": f"Bearer {search_key}"}
    if type == "web":
        url = "https://search.hackclub.com/res/v1/web/search"
        res = requests.get(url, params={"q": query}, headers=headers)
        data = res.json()
        return data.get('web', {}).get('results', [])
    elif type == "image":
        url = "https://search.hackclub.com/res/v1/images/search"
        res = requests.get(url, params={"q": query}, headers=headers)
        data = res.json()
        return data.get('results', [])
    elif type == "news":
        url = "https://search.hackclub.com/res/v1/news/search"
        res = requests.get(url, params={"q": query}, headers=headers)
        data = res.json()
        return data.get('news', {}).get('results', [])
    else:
        return None
@app.route('/api/savetest', methods=["POST"])
def savetest():
    incoming_data = request.json
    file_path = f'{root}user_data/{current_user.id}/stats.json'

    try:
        with open(file_path, 'r') as f:
            current_stats = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        current_stats = {"right": 0, "wrong": 0, "questions": []}

    current_stats['right'] += int(incoming_data.get('right', 0))
    current_stats['wrong'] += int(incoming_data.get('wrong', 0))
    
    if 'test' in incoming_data:
        current_stats['questions'].extend(incoming_data['test'])

    with open(file_path, 'w') as f:
        json.dump(current_stats, f, indent=4)

    return 'ok', 200
@app.route('/api/leaderboard')
def leaderboard():
    # Use os.path.join to handle Linux paths correctly
    root_dir = os.path.join(root, 'user_data') 
    
    # Load User DB
    try:
        with open(os.path.join(root, 'users.json'), 'r') as f:
            userDB = json.load(f)
    except Exception:
        return jsonify([]), 500

    leaderboard_list = []
    
    # Get all folder names in user_data
    if os.path.exists(root_dir):
        folders = [f for f in os.listdir(root_dir) if os.path.isdir(os.path.join(root_dir, f))]
        
        for x in folders:
            # 1. Look up user first (Outside the try block for the file)
            match = [u for u in userDB if u.get('id') == x]
            userOBJ = match[0] if match else None
            username = userOBJ['username'] if userOBJ else f"Unknown ({x[:5]})"

            # 2. Try to read the stats
            stats_path = os.path.join(root_dir, x, 'stats.json')
            
            if os.path.exists(stats_path):
                try:
                    with open(stats_path, 'r') as f:
                        data = json.load(f)
                        leaderboard_list.append({
                            "user": username,
                            "right": data.get('right', 0)
                        })
                except Exception:
                    # File exists but is corrupted
                    leaderboard_list.append({"user": username, "right": 0})
            else:
                # File doesn't exist (like your 6dffc84c folder)
                leaderboard_list.append({"user": username, "right": 0})

    # Sort: Highest score first
    leaderboard_list.sort(key=lambda x: x['right'], reverse=True)
    
    return jsonify(leaderboard_list), 200
@app.route('/api/delete')
def delete():
    name = request.args.get('name')
    data = readjson(f'user_data/{current_user.id}/cards.json')
    target = -1
    for x in range(len(data)):
        if data[x]["Title"] == name:
            target = x
            break
    data.pop(target)
    with open(f'user_data/{current_user.id}/cards.json', 'w') as f:
        json.dump(data, f, indent=4)
    return redirect(url_for('dash'))
    
@app.route('/')
@login_required
def home():
    user_agent = request.headers.get('User-Agent').lower()
    mobile_keywords = ['mobile', 'android', 'iphone', 'ipad', 'phone']
    is_mobile = any(keyword in user_agent for keyword in mobile_keywords)
    if is_mobile:
        return render_template('index-mobile.html')
    return render_template('index.html')

@app.route('/leaderboard')
@login_required
def leaderboard2():
    return render_template('leaderboard.html')

@app.route('/dash')
@login_required
def dash():
    user_agent = request.headers.get('User-Agent').lower()
    mobile_keywords = ['mobile', 'android', 'iphone', 'ipad', 'phone']
    is_mobile = any(keyword in user_agent for keyword in mobile_keywords)
    if is_mobile:
        return render_template('dash-mobile.html')
    return render_template('dash.html')

@app.route('/create')
@login_required
def create():
    return render_template('create.html')

# -- New Route for Parsing PDF --
@app.route("/api/parse-pdf", methods=["POST"])
@login_required
def parse_pdf():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['file']
    desc = request.form.get('desc')
    try:
        # Use the hybrid parser
        title, cards = parse_hybrid_quizlet_pdf(file)
        
        return jsonify([{
            "Title": title or "Imported Set",
            "cards": len(cards),
            "description": desc or "imported set from pdf",
            "content": cards
        }])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/import", methods=["POST"])
@login_required
def import_set():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "error": "No data received"}), 400

        # Save the deck to your cards.json or database here
        # For example, append to cards.json:
        import os, json
        cards_file = os.path.join(root, f'user_data/{current_user.id}/cards.json')
        if os.path.exists(cards_file):
            with open(cards_file, "r") as f:
                all_decks = json.load(f)
        else:
            all_decks = []

        all_decks.append(data)
        with open(cards_file, "w") as f:
            json.dump(all_decks, f, indent=4)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/viewcard')
@login_required
def viewcard():
    user_agent = request.headers.get('User-Agent').lower()
    mobile_keywords = ['mobile', 'android', 'iphone', 'ipad', 'phone']
    is_mobile = any(keyword in user_agent for keyword in mobile_keywords)
    if is_mobile:
        return render_template('viewcard-mobile.html')
    return render_template('viewcard.html')

@app.route('/api/cards')
@login_required
def get_cards():
    if not current_user.is_authenticated:
        return jsonify([])
    user_id = current_user.id
    cards = readjson(f'user_data/{user_id}/cards.json')
    return jsonify(cards)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        uid = str(uuid.uuid4())
        users = load_users()

        if any(u["username"] == username for u in users):
            return "User already exists"

        users.append({
            "id": uid,
            "username": username,
            "password": generate_password_hash(password)
        })

        with open(f"{root}users.json", "w") as f:
            json.dump(users, f, indent=4)
            try:
                os.makedirs(f'{root}user_data/{uid}', exist_ok=True)
            except OSError:
                pass
        return redirect(url_for("login"))

    return render_template("register.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        users = load_users()
        for u in users:
            if u["username"] == username and check_password_hash(u["password"], password):
                user = User(u["id"], u["username"], u["password"])
                login_user(user)
                return redirect(url_for("home"))

        return redirect(url_for("login", error="Invalid credentials"))

    return render_template("login.html")

if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')