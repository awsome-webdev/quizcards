from flask import Flask, request, jsonify, render_template, redirect, stream_with_context, url_for, Response, stream_with_context, send_file
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
model = ""
if os.path.exists('keys.json'):
    with open('keys.json') as f:
        keys = json.load(f)
        ai_key = keys[0]['hcai']
        search_key = keys[0]['hcsearch']
        model = keys[0]['model']

# Initialize AI Client if module exists
client = None
if OpenRouter:
    client = OpenRouter(
        api_key=ai_key,
        server_url="https://ai.hackclub.com/proxy/v1",
    )

import time

def ask(prompt):
    if not client: return "AI Client not initialized"
    
    try:
        response = client.chat.send(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        return response.choices[0].message.content
        
    except Exception as e:
        if "429" in str(e) or "rate limit" in str(e).lower():
            return "Rate limit reached. Please wait a moment."
        return f"An error occurred: {str(e)}"

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
import json
@app.route('/favicon')
def favicon():
    return send_file('favicon.png')
@app.route('/api/createwithai')
def createai():
    # 1. Grab all arguments before entering the generator context
    message = request.args.get('message')
    target_questions = request.args.get('target', 5)
    existingcards = request.args.get('cards', '[]')

    @stream_with_context
    def generate():
        # Validation
        if not message:
            yield f"data: {json.dumps({'error': 'No prompt provided'})}\n\n"
            return

        accumulated_context = ""
        max_iterations = 20

        try:
            for i in range(max_iterations):

                agent_prompt = f"""
                Topic: {message}
                Research so far: {accumulated_context if accumulated_context else "No data yet."}

                You are an AI agent that is for making educational flashcards for a flash card website. You must gather enough data to try and create {target_questions} more or start creating educational flashcards.
                These are the cards that already exist: {existingcards}
                Make sure to never duplicate or include already included info.
                You only have {max_iterations} iterations. This is iteration {i+1}.
                try to mostly use the information from the search to create the cards.

                INSTRUCTIONS:
                First, briefly write out your reasoning/thought process.
                Then, you MUST call exactly ONE of the following functions at the end of your response:

                1. search("your search query")
                   - Use this to get more info. Max 5 words.
                2. exit([{{ "question": "...", "answer": "...", "image": null }}, ...])
                   - Use this when you have sufficient info. Pass the JSON array of cards as the argument.
                3.fetch("url")
                    - Use this to fetch raw html from a website when you gather links from searches.
                4.respond("status update")
                    - Use this to send a snippet of text to the user to tell them what is happenning make sure to include this in all your responses so the user knows what you are doing.
                NEVER dont include a respond() in your response, this is how you will communicate to the user. Always include a respond() with a message about what you are doing. If you are searching include what you are searching for, if you are fetching include what you are fetching, if you are exiting include that you are exiting and how many cards you have created.
                Example Response:
                I need to find out the population of France to finish the last card.
                search("population of France")
                respond("Searching for population of France to finish card 5")
                """

                # Call your existing 'ask' function
                ai_response = ask(agent_prompt).strip()

                if "rate limit reached" in ai_response.lower():
                    yield f"data: {json.dumps({'error': 'Rate limited by AI provider'})}\n\n"
                    return

                # --- REGEX PARSING FOR FUNCTIONS ---
                # re.DOTALL ensures .* matches across multiple lines (crucial for JSON arrays)
                exit_match = re.search(r'exit\((.*)\)', ai_response, re.DOTALL)
                search_match = re.search(r'search\([\'"](.*?)[\'"]\)', ai_response)
                fetch_match = re.search(r'fetch\([\'"](.*?)[\'"]\)', ai_response)
                respond_match = re.search(r'respond\([\'"](.*?)[\'"]\)', ai_response)
                if not respond_match.group(1).strip():
                    respond_match = 'Thinking...'
                # OPTION 1: EXIT (Success)
                if exit_match:
                    raw_json = exit_match.group(1).strip()
                    # Extract the reasoning by removing the function call from the total string
                    reasoning = ai_response.replace(exit_match.group(0), "").strip()
                    

                    clean_json = raw_json.replace("```json", "").replace("```", "").strip()
                    
                    try:
                        card_set = json.loads(clean_json)
                        yield f"data: {json.dumps({'status': 'complete', 'cards': card_set})}\n\n"
                        return
                    except json.JSONDecodeError:
                        yield f"data: {json.dumps({'status': 'AI malformed JSON, retrying...'})}\n\n"
                        accumulated_context += "\nSystem Note: Your last exit() call had invalid JSON. Try again."
                        continue

                # OPTION 2: SEARCH
                elif search_match:
                    query = search_match.group(1).strip()
                    # Extract the reasoning
                    reasoning = ai_response.replace(search_match.group(0), "").strip()
                    
                    yield f"data: {json.dumps({'status': respond_match.group(1).strip()})}\n\n"
                    
                    search_results = search(query, type="web")
                    context = "\n\n".join(
                        f"[{r.get('title')}]({r.get('url')})\n{r.get('description')}" 
                        for r in search_results[:30]
                    )
                    accumulated_context += context
                    continue 
                # OPTION 3: FETCH
                elif fetch_match:
                    url = fetch_match.group(1).strip()
                    reasoning = ai_response.replace(fetch_match.group(0), "").strip()
                    
                    
                    yield f"data: {json.dumps({'status': respond_match.group(1).strip()})}\n\n"
                    
                    try:
                        res = requests.get(url, timeout=5)
                        if res.status_code == 200:
                            accumulated_context += f"\nFetched Content from {url}:\n{res.text[:500]}\n"  # Limit to first 500 chars
                        else:
                            accumulated_context += f"\nFailed to fetch {url}: Status code {res.status_code}\n"
                    except Exception as e:
                        accumulated_context += f"\nError fetching {url}: {str(e)}\n"
                    continue
                # Fallback: The AI forgot to call a function
                else:
                    yield f"data: {json.dumps({'status': 'AI formatting error, correcting...'})}\n\n"
                    accumulated_context += "\nSystem Note: You didn't call search(\"...\") or exit([...]). Please output a valid function call."
                    continue
            # If we exit the loop without returning (Max iterations reached)
            exitcards = ask(agent_prompt + "\n This is your last iteration. You MUST use the exit([...]) function with the cards in JSON format.")
            exit_match = re.search(r'exit\((.*)\)', exitcards, re.DOTALL)
            
            if exit_match:
                clean_json = exit_match.group(1).replace("```json", "").replace("```", "").strip()
                try:
                    card_set = json.loads(clean_json)
                    yield f"data: {json.dumps({'status': 'complete', 'cards': card_set})}\n\n"
                except:
                    yield f"data: {json.dumps({'error': 'Final output was not valid JSON.'})}\n\n"
            else:
                 yield f"data: {json.dumps({'error': 'Failed to generate cards within iteration limit.'})}\n\n"
            return

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')
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
    if incoming_data.get('percent'):
        current_stats.setdefault(incoming_data.get('setname'), []).append(incoming_data.get('percent'))
    
    if 'test' in incoming_data:
        current_stats['questions'].extend(incoming_data['test'])

    with open(file_path, 'w') as f:
        json.dump(current_stats, f, indent=4)

    return 'ok', 200
@app.route('/allsets')
@login_required
def allsets():
    return render_template('allsets.html')
@app.route('/blockblast')
@login_required
def blocks():
    return render_template('blockblast.html')
@app.route('/api/allcards')
def allcards():
    clear = request.args.get('clear')
    user = str(request.args.get('user'))
    title = str(request.args.get('title'))
    root_dir = os.path.join(root, 'user_data') 
    send = []
    try:
        with open(os.path.join(root, 'users.json'), 'r') as f:
            userDB = json.load(f)
    except Exception:
        return jsonify([]), 500

    leaderboard_list = []
    if os.path.exists(root_dir):
        folders = [f for f in os.listdir(root_dir) if os.path.isdir(os.path.join(root_dir, f))]
        
        for x in folders:
            match = [u for u in userDB if u.get('id') == x]
            userOBJ = match[0] if match else None
            username = userOBJ['username'] if userOBJ else f"Unknown ({x[:5]})"
            stats_path = os.path.join(root_dir, x, 'cards.json')
            if os.path.exists(stats_path):
                try:
                    with open(stats_path, 'r') as f:
                        data = json.load(f)
                        if (len(user) > 1 and len(title) > 1 and user != "None"):
                            if username == user:
                                for card in data:
                                    if card['Title'] == title:
                                        send.append(card)
                        else:
                            for card in data:
                                if clear:
                                    card['content'].clear()
                                card['name'] = username
                                send.append(card)
                except Exception as e:
                    return f'error {e}', 500
            else:
                continue
    return jsonify(send), 200
@app.route('/api/wrong')
def wrong():
    file = f'user_data/{current_user.id}/stats.json'
    data = readjson(file) 
    try:
        return jsonify(data['wrong'])
    except Exception as e:
        return '0'
@app.route('/api/right')
def right():
    file = f'user_data/{current_user.id}/stats.json'
    data = readjson(file)
    try:
        return jsonify(data['right'])
    except Exception as e:
        return '0'
@app.route('/api/getstats')
def getstats():
    file = f'user_data/{current_user.id}/stats.json'
    thres = 5 * 1024 * 1024
    try:
        file_size = os.path.getsize(file)
    except:
        file_size = 0
    if not file_size:
        return "{}", 200
    if file_size < thres:
            try:
                return jsonify(readjson(file))
            except Exception as e:
                return "{}", 200
    def generate():
        try:
            with open(f'{root}user_data/{current_user.id}/stats.json', 'rb') as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    yield chunk
            return Response(stream_with_context(generate()), mimetype='application/json')
        except:
            return '{}', 200
@app.route('/api/leaderboard')
def leaderboard():
    root_dir = os.path.join(root, 'user_data') 
    
    try:
        with open(os.path.join(root, 'users.json'), 'r') as f:
            userDB = json.load(f)
    except Exception:
        return jsonify([]), 500

    leaderboard_list = []
    if os.path.exists(root_dir):
        folders = [f for f in os.listdir(root_dir) if os.path.isdir(os.path.join(root_dir, f))]
        
        for x in folders:
            match = [u for u in userDB if u.get('id') == x]
            userOBJ = match[0] if match else None
            username = userOBJ['username'] if userOBJ else f"Unknown ({x[:5]})"
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
                    leaderboard_list.append({"user": username, "right": 0})
            else:
                leaderboard_list.append({"user": username, "right": 0})

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

@app.route("/api/parse-pdf", methods=["POST"])
@login_required
def parse_pdf():
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400
    
    file = request.files['file']
    desc = request.form.get('desc')
    try:
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

        target_set_title = request.args.get('set')

        import os, json
        cards_file = os.path.join(root, f'user_data/{current_user.id}/cards.json')
        
        if os.path.exists(cards_file):
            with open(cards_file, "r") as f:
                all_decks = json.load(f)
        else:
            all_decks = []


        if target_set_title:

            all_decks = [deck for deck in all_decks if deck.get("Title") != target_set_title]


        all_decks.append(data)

        with open(cards_file, "w") as f:
            json.dump(all_decks, f, indent=4)

        return jsonify({
            "status": "success", 
            "message": f"Updated {target_set_title}" if target_set_title else "Appended new set"
        }), 200

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
    try:
        clear = request.args.get('clear')
        cardset = request.args.get('set')
    except:
        pass
    if not current_user.is_authenticated:
        return jsonify([])
    user_id = current_user.id
    cards = readjson(f'user_data/{user_id}/cards.json')
    if cardset:
        target_content = next((item for item in cards if item["Title"] == cardset), None)
        return jsonify(target_content)
    if clear:
        cards[0]['content'].clear()
    return jsonify(cards)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        uid = str(uuid.uuid4())
        users = load_users()

        if any(u["username"] == username for u in users):
            return redirect(url_for('register', error='user already exist'))

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
    app.run(debug=True, port=5000, host='0.0.0.0', threaded=True)