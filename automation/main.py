import os
import json
import requests
import feedparser
import time
import random
from datetime import datetime
from slugify import slugify
from dotenv import load_dotenv
from io import BytesIO
from PIL import Image
import re # Tambahan untuk pembersih JSON

# --- 1. CONFIGURATION ---
load_dotenv()

# LOGIKA MULTI-API KEY
GROQ_KEYS_RAW = os.getenv("GROQ_API_KEY", "")
GROQ_API_KEYS = [k.strip() for k in GROQ_KEYS_RAW.split(",") if k.strip()]

if not GROQ_API_KEYS:
    print("❌ FATAL ERROR: Tidak ada API Key Groq ditemukan! Pastikan .env atau GitHub Secret sudah diisi.")
    exit(1)

# Target Berita (Google News US)
TARGET_CONFIG = {
    "rss_url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
}

# Direktori Folder
CONTENT_DIR = "content/articles"
IMAGE_DIR = "static/images"
DATA_DIR = "automation/data"
MEMORY_FILE = f"{DATA_DIR}/link_memory.json"
AUTHOR_NAME = "US News Desk"

# --- 2. SMART MEMORY SYSTEM ---

def load_link_memory():
    if not os.path.exists(MEMORY_FILE):
        return {}
    try:
        with open(MEMORY_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def save_link_to_memory(keyword, slug):
    os.makedirs(DATA_DIR, exist_ok=True)
    memory = load_link_memory()
    clean_key = keyword.lower().strip()
    memory[clean_key] = f"/articles/{slug}"
    with open(MEMORY_FILE, 'w') as f:
        json.dump(memory, f, indent=2)

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
            print("✅ Image Optimized & Saved.")
            return True
        else:
            print(f"❌ Image Download Failed. Status Code: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Image Error: {e}")
        return False

# --- 4. AI CONTENT ENGINE (FIXED 400 ERROR) ---

def clean_json_output(text):
    """Membersihkan output AI jika ada teks tambahan di luar kurung kurawal {}"""
    try:
        # Cari konten di antara kurung kurawal pertama dan terakhir
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return match.group(0)
        return text
    except:
        return text

def get_groq_article_seo(title, summary, link, internal_links_map):
    url = "https://api.groq.com/openai/v1/chat/completions"
    
    # PERBAIKAN: Gunakan Model Terbaru & Hapus response_format yang bikin error 400
    # Kita pakai llama-3.3-70b-versatile (Lebih pintar & stabil)
    MODEL_NAME = "llama-3.3-70b-versatile"
    
    system_prompt = """
    You are a Senior US Journalist.
    You MUST output valid JSON only. Do not add any markdown formatting like ```json ... ```.
    
    GUIDELINES:
    1. **Entity Salience**: Bold key entities.
    2. **Internal Linking**: Use provided memory. Link format: `[keyword](/articles/slug)`.
    3. **External Linking**: Include original source link.
    4. **Structure**: Catchy Headline, H2/H3 subheadings.
    """

    user_prompt = f"""
    SOURCE: "{title}"
    SUMMARY: "{summary}"
    LINK: {link}
    MEMORY: {internal_links_map}

    TASK: Write article in Markdown.
    OUTPUT JSON FORMAT:
    {{
        "title": "Title here",
        "content": "Markdown content here",
        "image_prompt": "Image description",
        "description": "Meta desc",
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
        "temperature": 0.5,
        # "response_format": {"type": "json_object"} <--- INI PENYEBAB ERROR 400, KITA HAPUS
    }

    # Rotasi Kunci
    for index, api_key in enumerate(GROQ_API_KEYS):
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        
        try:
            print(f"🤖 AI Writing... (Key #{index + 1})")
            response = requests.post(url, headers=headers, json=data)
            
            if response.status_code == 429:
                print(f"⚠️ Key #{index + 1} Limit Reached. Switching...")
                continue
            
            if response.status_code == 400:
                print(f"⚠️ Error 400 (Bad Request). Response: {response.text}")
                continue # Coba key lain atau skip
                
            response.raise_for_status()
            
            # Bersihkan JSON sebelum diparsing
            raw_content = response.json()['choices'][0]['message']['content']
            cleaned_content = clean_json_output(raw_content)
            return cleaned_content

        except Exception as e:
            print(f"⚠️ Error Key #{index + 1}: {e}")
            if index == len(GROQ_API_KEYS) - 1:
                print("❌ ALL KEYS FAILED.")
                return None
            continue

# --- 5. MAIN EXECUTION ---

def main():
    os.makedirs(CONTENT_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    print(f"📡 Fetching Google News US...")
    feed = feedparser.parse(TARGET_CONFIG['rss_url'])
    
    if not feed.entries:
        print("📭 No news found.")
        return

    entry = feed.entries[0]
    clean_title = entry.title.split(" - ")[0]
    file_slug = slugify(clean_title)
    filename = f"{file_slug}.md"

    if os.path.exists(f"{CONTENT_DIR}/{filename}"):
        print(f"⚠️ Exists: {clean_title}")
        return

    print(f"🔥 Processing: {clean_title}")
    
    context = get_internal_links_context()
    json_res = get_groq_article_seo(clean_title, entry.summary, entry.link, context)
    
    if not json_res: 
        print("❌ AI Generation Failed.")
        return
    
    try:
        data = json.loads(json_res)
    except json.JSONDecodeError:
        print("❌ JSON Parsing Failed. AI Output Invalid.")
        return

    image_filename = f"{file_slug}.jpg"
    has_image = download_and_optimize_image(data['image_prompt'], image_filename)
    final_image = f"/images/{image_filename}" if has_image else "/images/default-news.jpg"

    date_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-05:00")
    
    markdown_content = f"""---
title: "{data['title']}"
date: {date_now}
author: "{AUTHOR_NAME}"
categories: ["{data['category']}"]
tags: ["{data['main_keyword']}"]
featured_image: "{final_image}"
description: "{data['description']}"
draft: false
---

{data['content']}

---
*Sources:*
*   [Original Story]({entry.link})
*   *Analysis by {AUTHOR_NAME}*
"""

    with open(f"{CONTENT_DIR}/{filename}", "w", encoding="utf-8") as f:
        f.write(markdown_content)

    if 'main_keyword' in data and data['main_keyword']:
        save_link_to_memory(data['main_keyword'], file_slug)
        print(f"🧠 Memory: '{data['main_keyword']}' Saved.")
    
    print(f"✅ DONE: {filename}")

if __name__ == "__main__":
    main()