import os
import json
import requests
import time
import re
import random
import warnings 
import string
import pandas as pd
from datetime import datetime
from slugify import slugify
from io import BytesIO
from PIL import Image
from groq import Groq, APIError, RateLimitError
from pytrends.request import TrendReq

# --- SUPPRESS WARNINGS ---
warnings.filterwarnings("ignore", category=FutureWarning)

# ==========================================
# ⚙️ CONFIGURATION: US NEWS / GENERAL TRENDS
# ==========================================

# 🔑 API KEYS (GitHub Actions)
GROQ_KEYS_RAW = os.environ.get("GROQ_API_KEY", "") 
GROQ_API_KEYS = [k.strip() for k in GROQ_KEYS_RAW.split(",") if k.strip()]

if not GROQ_API_KEYS:
    print("❌ FATAL ERROR: Groq API Key is missing!")
    exit(1)

# 🔥 PERSONA PENULIS (News Desk)
AUTHOR_PROFILES = [
    "US News Desk", 
    "Tech Correspondent",
    "Market Analyst", 
    "Political Observer",
    "Entertainment Weekly"
]

# 📂 KATEGORI (News Niche)
VALID_CATEGORIES = [
    "US Politics", "Business", "Technology", 
    "Entertainment", "Sports", "Health",
    "Science", "World News"
]

# 📈 SEED KEYWORDS (Pancingan untuk US News Trends)
# Kita gunakan topik umum agar Pytrends mencarikan topik spesifik yang sedang "Naik Daun" (Rising)
SEED_KEYWORDS = [
    "Breaking News US", "Stock Market today", "US Politics", 
    "Viral Celebrity", "NBA News", "NFL News",
    "New Technology 2024", "Artificial Intelligence", "Health study",
    "Movie releases", "Crypto news", "White House"
]

CONTENT_DIR = "content/articles" 
IMAGE_DIR = "static/images"
DATA_DIR = "automation/data"
MEMORY_FILE = f"{DATA_DIR}/link_memory.json"

# Target Artikel per Run
TARGET_ARTICLES = 2

# ==========================================
# 🧠 HELPER FUNCTIONS
# ==========================================
def load_link_memory():
    if not os.path.exists(MEMORY_FILE): return {}
    try:
        with open(MEMORY_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_link_to_memory(title, slug):
    os.makedirs(DATA_DIR, exist_ok=True)
    memory = load_link_memory()
    memory[title] = f"/articles/{slug}/" 
    if len(memory) > 500: memory = dict(list(memory.items())[-500:])
    with open(MEMORY_FILE, 'w') as f: json.dump(memory, f, indent=2)

def fetch_trending_topics(keywords, max_results=3):
    """
    ADAPTED LOGIC: Mengambil 'Rising Queries' (Topik Naik Daun) 
    Metode ini lebih stabil daripada RealtimeTrends.
    """
    print(f"      ... Connecting to Google Trends...")
    topics = []
    
    # Backoff random
    time.sleep(random.uniform(2, 5))
    
    try:
        # --- FIX: Hapus parameter 'session', gunakan timeout saja ---
        pytrends = TrendReq(hl='en-US', tz=360, timeout=(10,25))
        
        # Ambil 1 Keyword Acak dari Seed
        current_kw = random.choice(keywords)
        print(f"      🔍 Analyzing Trends for Seed: '{current_kw}'")
        
        # Build Payload (Data 7 hari terakhir agar fresh)
        pytrends.build_payload([current_kw], cat=0, timeframe='now 7-d', geo='US', gprop='')
        
        # Ambil Related Queries
        related = pytrends.related_queries()
        
        if current_kw in related and related[current_kw]['rising'] is not None:
            df_rising = related[current_kw]['rising']
            
            # Ambil top queries yang relevan
            for index, row in df_rising.iterrows():
                query = row['query']
                # Filter query: minimal 2 kata
                if len(query.split()) >= 2: 
                    topics.append(query.title())
                    if len(topics) >= max_results:
                        break
            
            if len(topics) > 0:
                print(f"      ✅ Found {len(topics)} rising topics: {topics}")
                return topics
            
        print("      ⚠️ No significant 'Rising' data, using seed keyword.")
        return [current_kw]
            
    except Exception as e:
        print(f"      ⚠️ GTrends Error: {e}")
        # Fallback ke keyword itu sendiri jika API gagal
        return [current_kw]

def clean_ai_content(text):
    if not text: return ""
    text = re.sub(r'^```[a-zA-Z]*\n', '', text)
    text = re.sub(r'\n```$', '', text)
    text = text.replace("```", "")
    
    # Hapus Header Basa-basi
    patterns_to_remove = [
        r'^#+\s*Introduction.*?$', r'^#+\s*Conclusion.*?$', 
        r'^#+\s*Summary.*?$', r'^#+\s*The Verdict.*?$'
    ]
    for pattern in patterns_to_remove:
        text = re.sub(pattern, '', text, flags=re.MULTILINE | re.IGNORECASE)

    # Normalisasi Header
    text = text.replace("<h1>", "# ").replace("</h1>", "\n")
    text = text.replace("<h2>", "## ").replace("</h2>", "\n")
    text = text.replace("<h3>", "### ").replace("</h3>", "\n")
    
    return text.strip()

# ==========================================
# 📑 LINKS & FORMATTING
# ==========================================
def inject_links_into_body(content_body, current_title):
    memory = load_link_memory()
    items = list(memory.items())
    if not items: return content_body
    
    matches = random.sample(items, min(3, len(items)))
    
    link_box = "\n\n> **📰 Read Also:**\n"
    for title, url in matches:
        link_box += f"> - [{title}]({url})\n"
    link_box += "\n"

    paragraphs = content_body.split('\n\n')
    if len(paragraphs) > 3:
        paragraphs.insert(2, link_box)
        return "\n\n".join(paragraphs)
    return content_body + link_box

# ==========================================
# 🎨 IMAGE GENERATOR (MULTI-SOURCE / NO POLLINATIONS)
# ==========================================
def download_and_optimize_image(prompt, filename):
    output_path = f"{IMAGE_DIR}/{filename}"
    
    # Bersihkan prompt untuk tag pencarian Flickr
    clean_tags = prompt.replace(" ", ",").replace("photorealistic", "").replace("cinematic", "")
    clean_tags = re.sub(r'[^a-zA-Z,]', '', clean_tags)[:100]
    
    print(f"      🎨 Generating Image for: {clean_tags[:30]}...")
    
    # 1. Flickr (Real Photos - Prioritas Utama untuk Berita)
    flickr_url = f"https://loremflickr.com/1280/720/{clean_tags}/all"
    
    # 2. Hercai (AI Fallback)
    safe_prompt = prompt.replace(" ", "%20")[:200]
    hercai_url = f"https://hercai.onrender.com/v3/text2image?prompt={safe_prompt}"

    # 3. Picsum (Placeholder)
    picsum_url = "https://picsum.photos/1280/720"

    sources = [("Flickr", flickr_url), ("Hercai AI", hercai_url), ("Picsum", picsum_url)]

    for source_name, url in sources:
        try:
            if "Hercai" in source_name:
                resp = requests.get(url, timeout=30)
                if resp.status_code == 200:
                    json_data = resp.json()
                    if "url" in json_data:
                        image_url = json_data["url"]
                        img_resp = requests.get(image_url, timeout=30)
                    else: continue
                else: continue
            else:
                img_resp = requests.get(url, timeout=20, allow_redirects=True)

            if img_resp.status_code == 200:
                img = Image.open(BytesIO(img_resp.content))
                img = img.resize((1280, 720), Image.Resampling.LANCZOS)
                img.convert("RGB").save(output_path, "JPEG", quality=85, optimize=True)
                print(f"      ✅ Image Saved ({source_name})")
                return f"/images/{filename}"
        except Exception: continue
    
    return "/images/default-news.jpg"

# ==========================================
# 🧠 AI ENGINE (NEWS MODE)
# ==========================================
def get_groq_article_json(keyword, author_name):
    # System Prompt Khusus NEWS
    system_prompt = f"""
    You are {author_name}, a Senior Journalist for a major US Outlet.
    
    INPUT: Trending Keyword "{keyword}"
    TASK: Write a Breaking News Article / Analysis (1000+ words).
    
    STYLE RULES:
    1. Journalistic Tone (AP Style). Objective but engaging.
    2. Structure: 
       - Lead Paragraph (5W1H)
       - The Details (Body)
       - Background/Context
       - Reaction/Quotes (Simulated)
    3. NO "Introduction" or "Conclusion" headers. Use descriptive headers.
    
    OUTPUT JSON:
    {{
        "title": "A Clickworthy News Headline for '{keyword}'",
        "description": "SEO description (150 chars)",
        "category": "One of: {', '.join(VALID_CATEGORIES)}",
        "main_keyword": "{keyword}",
        "tags": ["tag1", "tag2", "News", "US"],
        "content_body": "Full markdown content..."
    }}
    """
    
    user_prompt = f"KEYWORD: {keyword}\n\nWrite the full story."
    
    for api_key in GROQ_API_KEYS:
        client = Groq(api_key=api_key)
        try:
            print(f"      🤖 AI Writing about '{keyword}'...")
            completion = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.6,
                max_tokens=6000,
                response_format={"type": "json_object"}
            )
            return completion.choices[0].message.content
        except RateLimitError:
            time.sleep(2)
        except Exception as e:
            print(f"      ⚠️ Error: {e}")
    return None

# ==========================================
# 🏁 MAIN WORKFLOW
# ==========================================
def main():
    os.makedirs(CONTENT_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    print("🔥 US NEWS ENGINE STARTED (ADAPTED MODE)")

    # 1. Fetch Trending Topics
    # Menggunakan Seed Keywords untuk mencari 'Rising Queries'
    trending_topics = fetch_trending_topics(SEED_KEYWORDS, max_results=TARGET_ARTICLES)
    
    processed_count = 0
    
    for topic in trending_topics:
        if processed_count >= TARGET_ARTICLES: break
        
        clean_topic = topic.strip()
        temp_slug = slugify(clean_topic, max_length=60)
        
        # Cek Duplikasi
        exists = False
        for f_name in os.listdir(CONTENT_DIR):
            if temp_slug in f_name:
                exists = True
                break
        
        if exists:
            print(f"   ⏩ Skipped (Exist): {clean_topic}")
            continue
            
        print(f"\n   ⚡ Processing Trend: {clean_topic}")
        
        author = random.choice(AUTHOR_PROFILES)
        raw_json = get_groq_article_json(clean_topic, author)
        
        if not raw_json: continue
        try:
            data = json.loads(raw_json)
        except:
            print("      ❌ JSON Error")
            continue

        # Finalize
        final_slug = slugify(data['title'], max_length=60)
        filename = f"{final_slug}.md"
        img_filename = f"{final_slug}.jpg"
        
        # Fallback Category
        cat = data.get('category', "General News")
        if cat not in VALID_CATEGORIES: cat = random.choice(VALID_CATEGORIES)

        # Generate Assets
        img_path = download_and_optimize_image(data['main_keyword'], img_filename)
        clean_body = clean_ai_content(data['content_body'])
        final_body = inject_links_into_body(clean_body, data['title'])
        
        # Create File
        md = f"""---
title: "{data['title'].replace('"', "'")}"
date: {datetime.now().strftime("%Y-%m-%dT%H:%M:%S+00:00")}
author: "{author}"
categories: ["{cat}"]
tags: {json.dumps(data.get('tags', []))}
featured_image: "{img_path}"
description: "{data['description'].replace('"', "'")}"
slug: "{final_slug}"
draft: false
---

{final_body}

---
*Sources: Analysis based on current trending topics.*
"""
        with open(f"{CONTENT_DIR}/{filename}", "w", encoding="utf-8") as f:
            f.write(md)
            
        save_link_to_memory(data['title'], final_slug)
        
        print(f"      ✅ Published: {final_slug}")
        processed_count += 1
        
        # Jeda antar artikel untuk keamanan
        print("      💤 Cooldown 30s...")
        time.sleep(30)

if __name__ == "__main__":
    main()
