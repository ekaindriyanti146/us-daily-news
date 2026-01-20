import os
import json
import requests
import feedparser
import time
import re
from datetime import datetime
from slugify import slugify
from io import BytesIO
from PIL import Image
from groq import Groq, APIError, RateLimitError, BadRequestError # SDK Resmi

# --- CONFIGURATION ---
# Tidak perlu load_dotenv di GitHub Actions karena pakai Secrets
# GROQ_API_KEY diambil dari Environment Variable
GROQ_KEYS_RAW = os.environ.get("GROQ_API_KEY", "")
GROQ_API_KEYS = [k.strip() for k in GROQ_KEYS_RAW.split(",") if k.strip()]

if not GROQ_API_KEYS:
    print("❌ FATAL ERROR: API Key Groq Kosong!")
    exit(1)

TARGET_CONFIG = {"rss_url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"}
CONTENT_DIR = "content/articles"
IMAGE_DIR = "static/images"
DATA_DIR = "automation/data"
MEMORY_FILE = f"{DATA_DIR}/link_memory.json"
AUTHOR_NAME = "US News Desk"

# --- MEMORY SYSTEM ---
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

# --- IMAGE ENGINE ---
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

# --- AI ENGINE (OFFICIAL GROQ SDK) ---
def clean_html(raw_html):
    cleanr = re.compile('<.*?>')
    return re.sub(cleanr, '', raw_html)

def get_groq_article_seo(title, summary, link, internal_links_map):
    clean_summary = clean_html(summary)
    MODEL_NAME = "llama-3.3-70b-versatile" # Model Terbaru
    
    system_prompt = """You are a US Journalist. Output JSON ONLY. 
    Structure: {"title": "...", "content": "Markdown...", "image_prompt": "...", "description": "...", "category": "...", "main_keyword": "..."}"""

    user_prompt = f"""News: {title}
    Summary: {clean_summary}
    Links: {internal_links_map}
    Task: Write full article in Markdown."""

    for index, api_key in enumerate(GROQ_API_KEYS):
        try:
            # Perhatikan kalimat print ini beda dengan log error Anda
            print(f"🤖 AI Writing with {MODEL_NAME}... (Key #{index+1})")
            
            client = Groq(api_key=api_key)
            completion = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.5,
                response_format={"type": "json_object"}
            )
            return completion.choices[0].message.content

        except BadRequestError as e:
            # Ini akan menangkap error 400 dan memberi tahu alasannya
            print(f"⚠️ GROQ 400 ERROR (Key #{index+1}): {e.body}")
            continue
        except Exception as e:
            print(f"⚠️ Error (Key #{index+1}): {e}")
            continue
            
    return None

# --- MAIN ---
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
        print("❌ AI Failed. Cek log.")
        return

    try:
        data = json.loads(json_res)
    except json.JSONDecodeError:
        print(f"❌ JSON Error. Raw: {json_res[:100]}")
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
