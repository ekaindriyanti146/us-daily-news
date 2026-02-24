import os
import json
import time
import re
import random
import requests
from datetime import datetime
from slugify import slugify
from io import BytesIO
from PIL import Image
from groq import Groq, APIError, RateLimitError, BadRequestError

# --- LIBRARY KHUSUS UNTUK FIX ERROR RETRY ---
from pytrends.request import TrendReq
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- CONFIGURATION (GITHUB ACTIONS) ---
GROQ_KEYS_RAW = os.environ.get("GROQ_API_KEY", "")
GROQ_API_KEYS = [k.strip() for k in GROQ_KEYS_RAW.split(",") if k.strip()]

if not GROQ_API_KEYS:
    print("❌ FATAL ERROR: Secret 'GROQ_API_KEY' Missing!")
    exit(1)

# NICHE: US NEWS / GENERAL TRENDS (TIDAK BERUBAH)
CATEGORY_MAPPING = {
    "Technology": "t",
    "Business": "b",
    "Entertainment": "e",
    "Sports": "s",
    "Health": "m",
    "Top Stories": "h"
}

CONTENT_DIR = "content/articles"
IMAGE_DIR = "static/images"
DATA_DIR = "automation/data"
MEMORY_FILE = f"{DATA_DIR}/link_memory.json"
AUTHOR_NAME = "Trend Desk"

TARGET_PER_CATEGORY = 1 

# ==========================================
# 🛠️ HELPER FUNCTIONS
# ==========================================

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
    items = list(memory.items())
    if len(items) > 20: items = random.sample(items, 20)
    return json.dumps(dict(items))

# ==========================================
# 📈 PYTRENDS DATA FETCHER (FIXED METHOD_WHITELIST ERROR)
# ==========================================
def fetch_pytrends_data(category_code, region='US'):
    print(f"      📡 Connecting to Google Trends (Cat: {category_code})...")
    try:
        # --- FIX: MANUAL SESSION SETUP ---
        # Kita membuat session sendiri agar tidak mengandalkan default pytrends yang error
        session = requests.Session()
        
        # Konfigurasi Retry manual dengan parameter yang benar ('allowed_methods')
        retry = Retry(
            total=3, 
            read=3, 
            connect=3, 
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            # INI KUNCI PERBAIKANNYA: Gunakan allowed_methods, bukan method_whitelist
            allowed_methods=["HEAD", "GET", "OPTIONS"] 
        )
        
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        # Masukkan session custom ke TrendReq
        pytrends = TrendReq(hl='en-US', tz=360, session=session)
        
        # Ambil Realtime Trends
        df = pytrends.realtime_trending_searches(pn=region, cat=category_code)
        
        if df is None or df.empty:
            print("      ⚠️ Empty Data from Google Trends.")
            return []
            
        trends_list = []
        # Ambil 3 teratas saja
        for index, row in df.head(3).iterrows():
            title = row['title']
            context = ""
            if 'entity_names' in row and isinstance(row['entity_names'], list):
                context = ", ".join(row['entity_names'])
            
            trends_list.append({
                "keyword": title,
                "context": context
            })
            
        return trends_list

    except Exception as e:
        # Jika error, print tapi jangan matikan script, lanjut kategori lain
        print(f"      ❌ Pytrends Error: {e}")
        return []

# ==========================================
# 🎨 IMAGE ENGINE (MULTI SOURCE - NO POLLINATIONS)
# ==========================================
def download_and_optimize_image(prompt, filename):
    output_path = f"{IMAGE_DIR}/{filename}"
    clean_tags = prompt.replace(" ", ",").replace("photorealistic", "").replace("cinematic", "")
    clean_tags = re.sub(r'[^a-zA-Z,]', '', clean_tags)[:100]
    
    # 1. Flickr
    flickr_url = f"https://loremflickr.com/1280/720/{clean_tags}/all"
    # 2. Hercai
    safe_prompt = prompt.replace(" ", "%20")[:200]
    hercai_url = f"https://hercai.onrender.com/v3/text2image?prompt={safe_prompt}"
    # 3. Picsum
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
                return True
        except Exception: continue
    return False

# ==========================================
# 🤖 GROQ CONTENT ENGINE
# ==========================================
def parse_ai_response(text):
    try:
        parts = text.split("|||BODY_START|||")
        if len(parts) < 2: return None
        json_part = parts[0].strip()
        body_part = parts[1].strip()
        json_part = re.sub(r'^```json', '', json_part)
        json_part = re.sub(r'```$', '', json_part)
        data = json.loads(json_part)
        data['content'] = body_part
        return data
    except Exception as e:
        print(f"      ❌ JSON Parse Error: {e}")
        return None

def get_groq_article(keyword, context_entities, internal_links, category):
    MODEL_NAME = "llama-3.3-70b-versatile"
    
    system_prompt = f"""
    You are a Senior Journalist.
    CATEGORY: {category}
    INPUT: Trending Keyword "{keyword}" (Context: {context_entities})
    
    TASK: Write a 800-1000 word news article.
    
    OUTPUT JSON:
    {{"title": "Headline using {keyword}", "description": "SEO desc", "category": "{category}", "main_keyword": "{keyword}", "image_prompt": "visual tags"}}
    |||BODY_START|||
    [Markdown Content]

    STRUCTURE:
    1. **Breaking News**: What is happening?
    2. **Background**: Context.
    3. **Impact**: Why it matters.
    Use H2 (##). Internal links: {internal_links}.
    """
    user_prompt = f"Write news about: {keyword}"

    for index, api_key in enumerate(GROQ_API_KEYS):
        try:
            client = Groq(api_key=api_key)
            completion = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.7, max_tokens=5000,
            )
            return completion.choices[0].message.content
        except Exception as e:
            print(f"      ⚠️ Groq Error (Key #{index}): {e}")
            continue
    return None

# ==========================================
# 🚀 MAIN
# ==========================================
def main():
    os.makedirs(CONTENT_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    print("🔥 ENGINE STARTED: GITHUB ACTIONS MODE (FIXED PYTRENDS)")

    total_generated = 0

    for category_name, cat_code in CATEGORY_MAPPING.items():
        print(f"\n📡 Fetching Pytrends: {category_name}...")
        
        # 1. Fetch Data (Fixed Session)
        trends = fetch_pytrends_data(cat_code)
        
        if not trends:
            print(f"   ⚠️ No trends data. Sleeping...")
            time.sleep(5)
            continue

        cat_success_count = 0
        for trend in trends:
            if cat_success_count >= TARGET_PER_CATEGORY: break

            keyword = trend['keyword']
            context = trend['context']
            clean_slug = slugify(keyword)
            filename = f"{clean_slug}.md"

            if os.path.exists(f"{CONTENT_DIR}/{filename}"): continue

            print(f"   🔥 Processing: {keyword}...")
            
            # 2. Content
            internal_links = get_internal_links_context()
            raw_response = get_groq_article(keyword, context, internal_links, category_name)
            if not raw_response: continue
            data = parse_ai_response(raw_response)
            if not data: continue

            # 3. Image
            img_name = f"{clean_slug}.jpg"
            has_img = download_and_optimize_image(data.get('image_prompt', keyword), img_name)
            final_img = f"/images/{img_name}" if has_img else "/images/default-news.jpg"
            
            # 4. Save
            date_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-05:00")
            md_content = f"""---
title: "{data['title'].replace('"', "'")}"
date: {date_str}
author: "{AUTHOR_NAME}"
categories: ["{data['category']}"]
tags: ["{data['main_keyword']}", "Trending"]
featured_image: "{final_img}"
description: "{data['description'].replace('"', "'")}"
draft: false
---

{data['content']}

---
*Sources: Analysis based on Realtime Trends data.*
"""
            with open(f"{CONTENT_DIR}/{filename}", "w", encoding="utf-8") as f: f.write(md_content)
            
            if 'main_keyword' in data: save_link_to_memory(data['main_keyword'], clean_slug)
            
            print(f"   ✅ Published: {filename}")
            cat_success_count += 1
            total_generated += 1
            
            # DELAY PENTING
            print("   💤 Cooling down 20s...")
            time.sleep(20)

    print(f"\n🎉 DONE! Total articles: {total_generated}")

if __name__ == "__main__":
    main()
