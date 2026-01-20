import os
import json
import requests
import feedparser
import time
import re
from datetime import datetime
from slugify import slugify
from dotenv import load_dotenv
from io import BytesIO
from PIL import Image

# --- 1. CONFIGURATION ---
load_dotenv()

# LOGIKA MULTI-API KEY
GROQ_KEYS_RAW = os.getenv("GROQ_API_KEY", "")
GROQ_API_KEYS = [k.strip() for k in GROQ_KEYS_RAW.split(",") if k.strip()]

if not GROQ_API_KEYS:
    print("❌ FATAL ERROR: API Key Groq Kosong!")
    exit(1)

# Target: Google News US
TARGET_CONFIG = {
    "rss_url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
}

# Folders
CONTENT_DIR = "content/articles"
IMAGE_DIR = "static/images"
DATA_DIR = "automation/data"
MEMORY_FILE = f"{DATA_DIR}/link_memory.json"
AUTHOR_NAME = "US News Desk"

# --- 2. MEMORY SYSTEM ---
def load_link_memory():
    if not os.path.exists(MEMORY_FILE): return {}
    try:
        with open(MEMORY_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_link_to_memory(keyword, slug):
    os.makedirs(DATA_DIR, exist_ok=True)
    memory = load_link_memory()
    clean_key = keyword.lower().strip()
    memory[clean_key] = f"/articles/{slug}"
    with open(MEMORY_FILE, 'w') as f: json.dump(memory, f, indent=2)

def get_internal_links_context():
    memory = load_link_memory()
    items = list(memory.items())[-50:] 
    return json.dumps(dict(items))

# --- 3. IMAGE ENGINE ---
def download_and_optimize_image(prompt, filename):
    safe_prompt = prompt.replace(" ", "%20")[:150]
    image_url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1280&height=720&nologo=true&model=flux"
    
    print(f"🎨 Generating Image: {filename}...")
    try:
        response = requests.get(image_url, timeout=30)
        if response.status_code == 200:
            img = Image.open(BytesIO(response.content))
            img = img.resize((1280, 720), Image.Resampling.LANCZOS)
            output_path = f"{IMAGE_DIR}/{filename}"
            img.convert("RGB").save(output_path, "JPEG", quality=75, optimize=True)
            print("✅ Image Saved.")
            return True
        return False
    except Exception as e:
        print(f"❌ Image Error: {e}")
        return False

# --- 4. AI ENGINE (BYPASS MODE) ---

def clean_json_output(text):
    """Membersihkan output AI dari Markdown code blocks"""
    try:
        # Hapus ```json dan ```
        text = re.sub(r'```json\s*', '', text)
        text = re.sub(r'```\s*', '', text)
        
        # Ambil hanya yang ada di dalam kurung kurawal {}
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return match.group(0)
        return text.strip()
    except:
        return text

def get_groq_article_seo(title, summary, link, internal_links_map):
    url = "https://api.groq.com/openai/v1/chat/completions"
    
    # Model Llama 3.3
    MODEL_NAME = "llama-3.3-70b-versatile"
    
    system_prompt = """
    You are an expert Journalist.
    You MUST output RAW JSON ONLY. Do not use Markdown blocks.
    Start directly with { and end with }.
    """

    user_prompt = f"""
    SOURCE: {title}
    SUMMARY: {summary}
    MEMORY: {internal_links_map}

    Task: Write a news article in Markdown.
    
    REQUIRED JSON FORMAT:
    {{
        "title": "Headline",
        "content": "Article body (use ## for headers)",
        "image_prompt": "Visual description",
        "description": "SEO meta",
        "category": "Technology/Business/Politics",
        "main_keyword": "Keyword"
    }}
    """
    
    data = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.5
        # "response_format": {"type": "json_object"} <--- INI KITA HAPUS TOTAL AGAR TIDAK ERROR 400
    }

    for index, api_key in enumerate(GROQ_API_KEYS):
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        
        try:
            print(f"🤖 AI Writing... (Key #{index+1})")
            response = requests.post(url, headers=headers, json=data)
            
            # --- DEBUGGING LENGKAP ---
            if response.status_code != 200:
                # Kita print pesan error aslinya dari Groq biar tahu kenapa
                print(f"⚠️ GROQ ERROR {response.status_code}: {response.text}")
            
            if response.status_code == 429:
                print("⚠️ Rate Limit. Switching key...")
                continue
            
            if response.status_code == 400:
                print("⚠️ Bad Request 400. Skipping key...")
                continue
                
            response.raise_for_status()
            
            # Ambil konten dan bersihkan manual
            raw_content = response.json()['choices'][0]['message']['content']
            return clean_json_output(raw_content)

        except Exception as e:
            print(f"⚠️ Error Key #{index+1}: {e}")
            continue
            
    return None

# --- 5. MAIN ---
def main():
    os.makedirs(CONTENT_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    print("📡 Fetching News...")
    feed = feedparser.parse(TARGET_CONFIG['rss_url'])
    if not feed.entries: return

    entry = feed.entries[0]
    clean_title = entry.title.split(" - ")[0]
    slug = slugify(clean_title)
    filename = f"{slug}.md"

    if os.path.exists(f"{CONTENT_DIR}/{filename}"):
        print(f"⚠️ Exists: {clean_title}")
        return

    print(f"🔥 Processing: {clean_title}")
    
    context = get_internal_links_context()
    json_res = get_groq_article_seo(clean_title, entry.summary, entry.link, context)
    
    if not json_res:
        print("❌ AI Failed (Check Logs).")
        return

    try:
        data = json.loads(json_res)
    except json.JSONDecodeError as e:
        print(f"❌ JSON Parse Error: {e}")
        # Jika error, kita lihat output mentahnya apa
        print(f"RAW OUTPUT: {json_res[:200]}...") 
        return

    img_name = f"{slug}.jpg"
    has_img = download_and_optimize_image(data['image_prompt'], img_name)
    final_img = f"/images/{img_name}" if has_img else "/images/default-news.jpg"
    
    date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-05:00")
    
    md = f"""---
title: "{data['title']}"
date: {date}
author: "{AUTHOR_NAME}"
categories: ["{data['category']}"]
tags: ["{data['main_keyword']}"]
featured_image: "{final_img}"
description: "{data['description']}"
draft: false
---

{data['content']}

---
*Source: [Original Story]({entry.link})*
"""
    with open(f"{CONTENT_DIR}/{filename}", "w", encoding="utf-8") as f: f.write(md)
    
    if 'main_keyword' in data: save_link_to_memory(data['main_keyword'], slug)
    print(f"✅ DONE: {filename}")

if __name__ == "__main__":
    main()