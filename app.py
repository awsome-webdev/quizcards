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

    # 1. Extract all images from the document
    for page in doc:
        for img in page.get_images(full=True):
            xref = img[0]
            base_image = doc.extract_image(xref)
            # Filter out tiny icons/bullet points
            if len(base_image["image"]) > 2000:
                b64 = base64.b64encode(base_image["image"]).decode('utf-8')
                extracted_images.append(f"data:image/{base_image['ext']};base64,{b64}")

    # 2. Extract all text blocks
    full_text = ""
    for page in doc:
        full_text += page.get_text() + "\n"

    # Split by Quizlet's numbering (e.g., "1. ", "2. ")
    raw_segments = re.split(r'\n\d+\.\s+', full_text)
    title = raw_segments[0].split('\n')[0].strip() if raw_segments else "Imported Set"
    
    for segment in raw_segments[1:]:
        lines = [l.strip() for l in segment.split('\n') if l.strip()]
        if lines:
            text_blocks.append(lines)

    # 3. Decision Engine: How do we pair them?
    final_cards = []
    
    # Case A: We have images that likely correspond to the text
    if len(extracted_images) >= len(text_blocks) and len(text_blocks) > 0:
        for i in range(len(text_blocks)):
            final_cards.append({
                "question": "", 
                "image": extracted_images[i], # Image is the Question
                "answer": " <br> ".join(text_blocks[i]) # All text is the Answer
            })
            
    # Case B: No images, or purely text-based Quizlet set
    else:
        for lines in text_blocks:
            final_cards.append({
                "question": lines[0], # First line is Question
                "image": None,
                "answer": " <br> ".join(lines[1:]) if len(lines) > 1 else "No definition"
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

# --- Routes ---

@app.route('/')
@login_required
def home():
    return render_template('index.html')

@app.route('/dash')
def dash():
    return render_template('dash.html')

@app.route('/create')
def create():
    return render_template('create.html')

# -- New Route for Parsing PDF --
@app.route("/api/parse-pdf", methods=["POST"])
@login_required
def parse_pdf():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['file']
    try:
        # Use the hybrid parser
        title, cards = parse_hybrid_quizlet_pdf(file)
        
        return jsonify([{
            "Title": title or "Imported Set",
            "cards": len(cards),
            "description": "Flashcards automatically detected and paired.",
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
def viewcard():
    return render_template('viewcard.html')

@app.route('/api/cards')
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
    app.run(debug=True, port=5000)