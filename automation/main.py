import os
import json
import requests
import feedparser
import time
import re
import random
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

TARGET_CONFIG = {"rss_url": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"}
CONTENT_DIR = "content/articles"
IMAGE_DIR = "static/images"
DATA_DIR = "automation/data"
MEMORY_FILE = f"{DATA_DIR}/link_memory.json"
AUTHOR_NAME = "US News Desk"
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
    # Ambil 30 link acak agar konteks selalu segar
    items = list(memory.items())
    if len(items) > 30:
        items = random.sample(items, 30)
    return json.dumps(dict(items))

# --- ROBUST IMAGE ENGINE (RETRY MODE) ---
def download_and_optimize_image(prompt, filename):
    safe_prompt = prompt.replace(" ", "%20")[:200]
    # Menggunakan model Flux Realism
    image_url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1280&height=720&nologo=true&model=flux-realism"
    
    # RETRY LOGIC (3x Percobaan)
    for attempt in range(3):
        try:
            print(f"   🎨 Generating Image (Attempt {attempt+1})...")
            response = requests.get(image_url, timeout=60) # Naikkan timeout ke 60s
            if response.status_code == 200:
                img = Image.open(BytesIO(response.content))
                img = img.resize((1280, 720), Image.Resampling.LANCZOS)
                output_path = f"{IMAGE_DIR}/{filename}"
                img.convert("RGB").save(output_path, "JPEG", quality=80, optimize=True)
                return True
        except Exception as e:
            print(f"      ⚠️ Image fail: {e}")
            time.sleep(5) # Tunggu 5 detik sebelum coba lagi
    
    print("   ❌ Image failed after 3 attempts.")
    return False

# --- AI ENGINE (TEXT PARSER) ---
# Kita tidak pakai JSON untuk konten panjang, tapi Custom Separator agar aman
def parse_ai_response(text):
    try:
        # Pisahkan Header dan Body
        parts = text.split("|||BODY_START|||")
        if len(parts) < 2: return None
        
        json_part = parts[0].strip()
        body_part = parts[1].strip()
        
        # Bersihkan JSON dari markdown formatting ```json ... ```
        json_part = re.sub(r'```json\s*', '', json_part)
        json_part = re.sub(r'```', '', json_part)
        
        data = json.loads(json_part)
        data['content'] = body_part
        return data
    except Exception as e:
        print(f"   ❌ Parse Error: {e}")
        return None

def get_groq_article_seo(title, summary, link, internal_links_map):
    MODEL_NAME = "llama-3.3-70b-versatile"
    
    # PROMPT EEAT 2026 (STRUCTURED DATA & DEPTH)
    system_prompt = """
    You are a Pulitzer-winning Senior US Journalist.
    
    TASK: Write a highly authoritative, deep-dive news analysis (1200+ words).
    
    OUTPUT FORMAT (STRICT):
    {JSON_METADATA}
    |||BODY_START|||
    [Markdown Article Content]

    METADATA JSON FIELDS:
    - title: (Clickbait but factual)
    - description: (SEO meta description, 150 chars)
    - category: (One word: Politics, Business, Tech, World, Health)
    - main_keyword: (Focus keyword)
    - image_prompt: (Cinematic, photorealistic description, 16:9)

    ARTICLE STRUCTURE (Markdown):
    1. **Key Takeaways** (Bulleted list at the top, bold important stats).
    2. **Introduction** (The Hook: What happened, Who, When, Where).
    3. **The Deep Dive** (Detailed background, context).
    4. **Critical Analysis** (Why this matters? connect to US Economy/Policy).
    5. **Expert Opinions** (Simulate relevant quotes from officials/experts).
    6. **Future Outlook** (What happens next?).
    
    SEO RULES:
    - Use H2 (##) and H3 (###) for hierarchy.
    - **BOLD** important entities (Names, Organizations, Cities).
    - Insert internal links from this list naturally: {LINKS} -> Syntax: [Keyword](/articles/slug).
    - Do NOT use phrases like "In conclusion" or "In summary". Use "Final Thoughts" or "Looking Ahead".
    """

    user_prompt = f"""
    News Event: {title}
    Context: {summary}
    Internal Links Available: {internal_links_map}
    
    Write the article now. Be critical and objective.
    """

    for index, api_key in enumerate(GROQ_API_KEYS):
        try:
            print(f"   🤖 AI Analysis & Writing (Key #{index+1})...")
            
            client = Groq(api_key=api_key)
            completion = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt.replace("{LINKS}", internal_links_map)},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.6,
                max_tokens=7000, # Max token besar untuk artikel panjang
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

    print("📡 Fetching High-Quality Sources...")
    feed = feedparser.parse(TARGET_CONFIG['rss_url'])
    
    if not feed.entries: return

    success_count = 0
    print(f"🎯 Mission: Publish {TARGET_ARTICLE_COUNT} Deep-Dive Articles.")

    for entry in feed.entries:
        if success_count >= TARGET_ARTICLE_COUNT: break

        clean_title = entry.title.split(" - ")[0]
        slug = slugify(clean_title)
        filename = f"{slug}.md"

        if os.path.exists(f"{CONTENT_DIR}/{filename}"):
            print(f"⏭️  Exists: {clean_title[:30]}...")
            continue

        print(f"\n🔥 Processing [{success_count + 1}/{TARGET_ARTICLE_COUNT}]: {clean_title}")
        
        # 1. Generate Content (Raw Text)
        context = get_internal_links_context()
        raw_response = get_groq_article_seo(clean_title, entry.summary, entry.link, context)
        
        if not raw_response:
            print("   ❌ AI Generation Failed.")
            continue

        # 2. Parse Split (JSON Header vs Markdown Body)
        data = parse_ai_response(raw_response)
        if not data:
            print("   ❌ Parsing Failed (Structure invalid).")
            continue

        # 3. Image Engine (Retry Enabled)
        img_name = f"{slug}.jpg"
        has_img = download_and_optimize_image(data['image_prompt'], img_name)
        final_img = f"/images/{img_name}" if has_img else "/images/default-news.jpg"
        
        # 4. Save Markdown
        date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-05:00")
        
        md = f"""---
title: "{data['title'].replace('"', "'")}"
date: {date}
author: "{AUTHOR_NAME}"
categories: ["{data['category']}"]
tags: ["{data['main_keyword']}"]
featured_image: "{final_img}"
description: "{data['description'].replace('"', "'")}"
draft: false
---

{data['content']}

---
*Sources: Analysis based on reports from AP, Reuters, and [Original Story]({entry.link}).*
"""
        with open(f"{CONTENT_DIR}/{filename}", "w", encoding="utf-8") as f: f.write(md)
        
        if 'main_keyword' in data: save_link_to_memory(data['main_keyword'], slug)
        
        print(f"   ✅ Published: {filename}")
        success_count += 1
        
        # Jeda aman
        if success_count < TARGET_ARTICLE_COUNT:
            print("   zzz... Analyzing next topic (10s)...")
            time.sleep(10)

if __name__ == "__main__":
    main()
