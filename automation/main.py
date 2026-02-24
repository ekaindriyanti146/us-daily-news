import os
import json
import time
import re
import random
from datetime import datetime
import requests
from slugify import slugify
from io import BytesIO
from PIL import Image
from groq import Groq, APIError, RateLimitError, BadRequestError

# Library Pytrends (Wajib ada di requirements.txt)
from pytrends.request import TrendReq

# Library Dotenv (Opsional di GitHub Actions, tapi tetap kita load agar tidak error jika ada di requirements)
try:
    from dotenv import load_dotenv
    load_dotenv() # Aman dipanggil meski file .env tidak ada
except ImportError:
    pass

# ==========================================
# ⚙️ CONFIGURATION (GITHUB ACTIONS CONTEXT)
# ==========================================

# Mengambil Key langsung dari Environment Variable (GitHub Secrets)
GROQ_KEYS_RAW = os.environ.get("GROQ_API_KEY", "")
GROQ_API_KEYS = [k.strip() for k in GROQ_KEYS_RAW.split(",") if k.strip()]

if not GROQ_API_KEYS:
    print("❌ FATAL ERROR: Secret 'GROQ_API_KEY' tidak ditemukan di Environment GitHub Actions!")
    exit(1)

# Mapping Kategori Pytrends (Realtime)
# 'b': Business, 'e': Entertainment, 'm': Health, 's': Sports, 't': Sci/Tech, 'h': Top Stories
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

# Target artikel per kategori sekali jalan
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
    # Ambil sampel acak agar prompt tidak terlalu panjang
    if len(items) > 20:
        items = random.sample(items, 20)
    return json.dumps(dict(items))

# ==========================================
# 📈 PYTRENDS DATA FETCHER
# ==========================================
def fetch_pytrends_data(category_code, region='US'):
    print(f"      📡 Connecting to Google Trends (Cat: {category_code})...")
    try:
        # Inisialisasi Pytrends
        # hl='en-US' penting agar keyword keluar dalam bahasa Inggris
        pytrends = TrendReq(hl='en-US', tz=360, timeout=(10,25), retries=3, backoff_factor=0.5)
        
        # Ambil Realtime Trends
        df = pytrends.realtime_trending_searches(pn=region, cat=category_code)
        
        if df is None or df.empty:
            print("      ⚠️ Empty Data from Google Trends.")
            return []
            
        trends_list = []
        # Ambil 5 teratas saja untuk diproses
        for index, row in df.head(5).iterrows():
            title = row['title'] # Keyword Utama
            
            # Ambil konteks entities jika ada
            context = ""
            if 'entity_names' in row and isinstance(row['entity_names'], list):
                context = ", ".join(row['entity_names'])
            
            trends_list.append({
                "keyword": title,
                "context": context
            })
            
        return trends_list

    except Exception as e:
        print(f"      ❌ Pytrends Error (Mungkin Blocked IP GitHub): {e}")
        return []

# ==========================================
# 🎨 IMAGE ENGINE (NO POLLINATIONS - ROBUST)
# ==========================================
def download_and_optimize_image(prompt, filename):
    output_path = f"{IMAGE_DIR}/{filename}"
    
    # Bersihkan prompt untuk tag pencarian Flickr
    clean_tags = prompt.replace(" ", ",").replace("photorealistic", "").replace("cinematic", "")
    clean_tags = re.sub(r'[^a-zA-Z,]', '', clean_tags)[:100]
    
    # Sumber 1: Flickr (Real Photos)
    flickr_url = f"https://loremflickr.com/1280/720/{clean_tags}/all"
    
    # Sumber 2: Hercai (AI Fallback)
    safe_prompt = prompt.replace(" ", "%20")[:200]
    hercai_url = f"https://hercai.onrender.com/v3/text2image?prompt={safe_prompt}"

    # Sumber 3: Picsum (Final Fallback)
    picsum_url = "https://picsum.photos/1280/720"

    sources = [
        ("Flickr", flickr_url),
        ("Hercai AI", hercai_url),
        ("Picsum", picsum_url)
    ]

    for source_name, url in sources:
        try:
            # print(f"      🎨 Trying Image: {source_name}...")
            
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
                
        except Exception:
            continue
    
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
    
    TASK: Write a 800-1000 word news article explaining this trend.
    
    OUTPUT FORMAT (STRICT JSON + BODY):
    {{"title": "Engaging Headline containing {keyword}", "description": "SEO description (150 chars)", "category": "{category}", "main_keyword": "{keyword}", "image_prompt": "visual tags for flickr search (e.g. {keyword}, press conference)"}}
    |||BODY_START|||
    [Markdown Article Content]

    STRUCTURE:
    1. **Breaking News**: What is happening?
    2. **Background**: Context/History.
    3. **Impact**: Why it matters.
    
    Use H2 (##). Use internal links: {internal_links}.
    """

    user_prompt = f"Write news about: {keyword}"

    for index, api_key in enumerate(GROQ_API_KEYS):
        try:
            print(f"      🤖 Generating Article for: {keyword}...")
            client = Groq(api_key=api_key)
            completion = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=5000,
            )
            return completion.choices[0].message.content

        except Exception as e:
            print(f"      ⚠️ Groq Error (Key #{index}): {e}")
            continue
            
    return None

# ==========================================
# 🚀 MAIN EXECUTION
# ==========================================
def main():
    # Buat folder jika belum ada
    os.makedirs(CONTENT_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    print("🔥 ENGINE STARTED: GITHUB ACTIONS MODE (PYTRENDS)")

    total_generated = 0

    for category_name, cat_code in CATEGORY_MAPPING.items():
        print(f"\n📡 Fetching Pytrends: {category_name}...")
        
        # 1. Ambil data dari Pytrends
        trends = fetch_pytrends_data(cat_code)
        
        if not trends:
            print(f"   ⚠️ No trends data. Skipping {category_name}...")
            # Jeda random agar tidak dikira bot spammer terus-terusan
            time.sleep(random.randint(5, 10))
            continue

        cat_success_count = 0
        
        for trend in trends:
            if cat_success_count >= TARGET_PER_CATEGORY:
                break

            keyword = trend['keyword']
            context = trend['context']
            
            clean_slug = slugify(keyword)
            filename = f"{clean_slug}.md"

            # Cek duplikat
            if os.path.exists(f"{CONTENT_DIR}/{filename}"):
                continue

            print(f"   🔥 Processing: {keyword}...")
            
            # 2. Generate Content
            internal_links = get_internal_links_context()
            raw_response = get_groq_article(keyword, context, internal_links, category_name)
            
            if not raw_response: continue

            data = parse_ai_response(raw_response)
            if not data: continue

            # 3. Generate Image (Multi-source)
            img_name = f"{clean_slug}.jpg"
            img_prompt = data.get('image_prompt', keyword)
            has_img = download_and_optimize_image(img_prompt, img_name)
            
            final_img = f"/images/{img_name}" if has_img else "/images/default-news.jpg"
            
            # 4. Save Markdown
            date_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-05:00")
            md_content = f"""---
title: "{data['title'].replace('"', "'")}"
date: {date_str}
author: "{AUTHOR_NAME}"
categories: ["{data['category']}"]
tags: ["{data['main_keyword']}", "Trending"]
featured_image: "{final_img}"
description: "{data['description'].replace('"', "'")}"
slug: "{slug}"
url: "/{slug}/"
draft: false
---

{data['content']}

---
*Sources: Analysis based on Realtime Trends data.*
"""
            with open(f"{CONTENT_DIR}/{filename}", "w", encoding="utf-8") as f:
                f.write(md_content)
            
            if 'main_keyword' in data: 
                save_link_to_memory(data['main_keyword'], clean_slug)
            
            print(f"   ✅ Published: {filename}")
            cat_success_count += 1
            total_generated += 1
            
            # CRITICAL: Delay untuk menghindari ban IP di GitHub Actions
            print("   💤 Cooling down 20s...")
            time.sleep(30)

    print(f"\n🎉 DONE! Total articles: {total_generated}")

if __name__ == "__main__":
    main()
