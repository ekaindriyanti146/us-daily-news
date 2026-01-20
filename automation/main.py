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
from groq import Groq, APIError, RateLimitError, BadRequestError

# --- CONFIGURATION ---
GROQ_KEYS_RAW = os.environ.get("GROQ_API_KEY", "")
GROQ_API_KEYS = [k.strip() for k in GROQ_KEYS_RAW.split(",") if k.strip()]

if not GROQ_API_KEYS:
    print("❌ FATAL ERROR: API Key Groq Kosong!")
    exit(1)

# Target Google News US
TARGET_CONFIG = {"rss_url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"}

CONTENT_DIR = "content/articles"
IMAGE_DIR = "static/images"
DATA_DIR = "automation/data"
MEMORY_FILE = f"{DATA_DIR}/link_memory.json"
AUTHOR_NAME = "US News Desk"

# TARGET JUMLAH ARTIKEL PER JALAN (CRON)
TARGET_ARTICLE_COUNT = 5 

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
    # Prompt gambar dipersingkat agar tidak error di URL
    safe_prompt = prompt.replace(" ", "%20")[:180] 
    image_url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1280&height=720&nologo=true&model=flux"
    print(f"   🎨 Generating Image...")
    try:
        response = requests.get(image_url, timeout=30)
        if response.status_code == 200:
            img = Image.open(BytesIO(response.content))
            img = img.resize((1280, 720), Image.Resampling.LANCZOS)
            output_path = f"{IMAGE_DIR}/{filename}"
            img.convert("RGB").save(output_path, "JPEG", quality=75, optimize=True)
            return True
        return False
    except Exception as e:
        print(f"   ❌ Image Error: {e}")
        return False

# --- AI ENGINE (OFFICIAL GROQ SDK) ---
def clean_html(raw_html):
    cleanr = re.compile('<.*?>')
    return re.sub(cleanr, '', raw_html)

def get_groq_article_seo(title, summary, link, internal_links_map):
    clean_summary = clean_html(summary)
    MODEL_NAME = "llama-3.3-70b-versatile"
    
    # PROMPT KHUSUS AGAR ARTIKEL PANJANG (1000+ KATA)
    # Kita memaksa AI membagi konten menjadi banyak sub-bagian
    system_prompt = """
    You are a Senior US Political & Economic Analyst. 
    Your Task: Write a comprehensive, deep-dive article (minimum 1000 words).
    
    GUIDELINES:
    1. Output JSON ONLY. Format: {"title": "...", "content": "markdown...", "image_prompt": "...", "description": "...", "category": "...", "main_keyword": "..."}
    2. Tone: Professional, investigative, objective, and authoritative.
    3. Structure (Must be included in 'content' Markdown):
       - Introduction (Hook the reader)
       - Historical Context (Background info)
       - Detailed Analysis (The core of the news)
       - Key Players & Reactions (Quotes/Perspectives)
       - Economic/Political Implications (Future outlook)
       - Conclusion
    4. Internal Linking: Use the provided 'Links' JSON to insert markdown links [keyword](/articles/slug) NATURALLY in the text.
    5. Formatting: Use H2 (##) for section headers. Use bold for entities.
    """

    user_prompt = f"""
    News Title: {title}
    Source Summary: {clean_summary}
    Existing Internal Links: {internal_links_map}
    
    INSTRUCTION: 
    Expand this news into a full feature story. Do not be brief. 
    Elaborate on every point. 
    Analyze the "Why" and "How".
    Create a highly detailed image prompt for the article cover.
    """

    for index, api_key in enumerate(GROQ_API_KEYS):
        try:
            print(f"   🤖 AI Writing (Key #{index+1})... This may take 30s...")
            
            client = Groq(api_key=api_key)
            completion = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.6, # Sedikit kreatif agar panjang
                max_tokens=6000, # Allow output panjang
                response_format={"type": "json_object"}
            )
            return completion.choices[0].message.content

        except BadRequestError as e:
            print(f"   ⚠️ GROQ 400 ERROR: {e.body}")
            continue
        except Exception as e:
            print(f"   ⚠️ Error (Key #{index+1}): {e}")
            continue
            
    return None

# --- MAIN LOOP ---
def main():
    os.makedirs(CONTENT_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    print("📡 Fetching Google News RSS...")
    feed = feedparser.parse(TARGET_CONFIG['rss_url'])
    
    if not feed.entries:
        print("❌ No entries found in RSS.")
        return

    success_count = 0
    print(f"🎯 Target: Generate {TARGET_ARTICLE_COUNT} articles.")

    # LOOPING ARTIKEL
    for entry in feed.entries:
        # Jika sudah mencapai target 5 artikel, berhenti
        if success_count >= TARGET_ARTICLE_COUNT:
            print("✅ Target reached. Stopping.")
            break

        clean_title = entry.title.split(" - ")[0]
        slug = slugify(clean_title)
        filename = f"{slug}.md"

        # Cek apakah artikel sudah ada agar tidak duplikat
        if os.path.exists(f"{CONTENT_DIR}/{filename}"):
            print(f"⏭️  Skipping (Exists): {clean_title[:30]}...")
            continue

        print(f"\n🔥 Processing [{success_count + 1}/{TARGET_ARTICLE_COUNT}]: {clean_title}")
        
        # 1. Generate Konten
        context = get_internal_links_context()
        json_res = get_groq_article_seo(clean_title, entry.summary, entry.link, context)
        
        if not json_res:
            print("   ❌ AI Failed to generate content.")
            continue

        try:
            data = json.loads(json_res)
        except json.JSONDecodeError:
            print("   ❌ JSON Parsing Error.")
            continue

        # 2. Generate Gambar
        img_name = f"{slug}.jpg"
        has_img = download_and_optimize_image(data['image_prompt'], img_name)
        final_img = f"/images/{img_name}" if has_img else "/images/default-news.jpg"
        
        # 3. Save Markdown
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
        
        # 4. Save Memory
        if 'main_keyword' in data: 
            save_link_to_memory(data['main_keyword'], slug)
        
        print(f"   ✅ Saved: {filename}")
        success_count += 1

        # 5. Rate Limit Safety (PENTING)
        # Istirahat 15 detik sebelum artikel berikutnya agar tidak kena limit Groq/Pollinations
        if success_count < TARGET_ARTICLE_COUNT:
            print("   zzz... Sleeping 15s (Rate Limit Safety)...")
            time.sleep(15)

if __name__ == "__main__":
    main()
