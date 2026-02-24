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

# DAFTAR KATEGORI & RSS URL GOOGLE TRENDS (US REGION)
# Kode Kategori GTRENDS: b=Business, e=Entertainment, m=Health, s=Sports, t=Sci/Tech, h=Top
BASE_GTRENDS = "https://trends.google.com/trends/trendingsearches/daily/rss?geo=US"

CATEGORY_URLS = {
    "General Trends": f"{BASE_GTRENDS}", # Campuran (untuk Politik/World biasanya masuk sini)
    "Business": f"{BASE_GTRENDS}&cat=b",
    "Technology": f"{BASE_GTRENDS}&cat=t",
    "Health": f"{BASE_GTRENDS}&cat=m",
    "Entertainment": f"{BASE_GTRENDS}&cat=e",
    "Sports": f"{BASE_GTRENDS}&cat=s"
}

CONTENT_DIR = "content/articles"
IMAGE_DIR = "static/images"
DATA_DIR = "automation/data"
MEMORY_FILE = f"{DATA_DIR}/link_memory.json"
AUTHOR_NAME = "Trend Analyst"

# Target per kategori
TARGET_PER_CATEGORY = 1 

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
    items = list(memory.items())
    if len(items) > 30:
        items = random.sample(items, 30)
    return json.dumps(dict(items))

# --- IMAGE ENGINE (ROBUST - NO POLLINATIONS) ---
def download_and_optimize_image(prompt, filename):
    """
    Menggunakan LoremFlickr (Real Photos) sebagai prioritas,
    fallback ke Hercai (AI) jika gagal.
    """
    output_path = f"{IMAGE_DIR}/{filename}"
    
    # Bersihkan prompt untuk tag URL
    clean_tags = prompt.replace(" ", ",").replace("photorealistic", "").replace("cinematic", "")
    clean_tags = re.sub(r'[^a-zA-Z,]', '', clean_tags)[:100] # Ambil huruf & koma saja
    
    # 1. SUMBER FLICKR (Via LoremFlickr - Real Photos)
    # Format: https://loremflickr.com/width/height/tags
    flickr_url = f"https://loremflickr.com/1280/720/{clean_tags}/all"
    
    # 2. SUMBER AI ALTERNATIF (Hercai - Text to Image)
    safe_prompt = prompt.replace(" ", "%20")[:200]
    hercai_url = f"https://hercai.onrender.com/v3/text2image?prompt={safe_prompt}"

    # 3. SUMBER PLACEHOLDER (Picsum)
    picsum_url = "https://picsum.photos/1280/720"

    # List urutan percobaan
    sources = [
        ("Flickr (Real)", flickr_url),
        ("Hercai (AI)", hercai_url),
        ("Picsum (Backup)", picsum_url)
    ]

    for source_name, url in sources:
        try:
            print(f"      🎨 Trying Image Source: {source_name}...")
            
            # Khusus Hercai butuh parsing JSON
            if "Hercai" in source_name:
                resp = requests.get(url, timeout=40)
                if resp.status_code == 200:
                    json_data = resp.json()
                    if "url" in json_data:
                        image_url = json_data["url"]
                        img_resp = requests.get(image_url, timeout=30)
                    else: continue
                else: continue
            else:
                # Flickr/Picsum langsung return image binary
                img_resp = requests.get(url, timeout=30, allow_redirects=True)

            if img_resp.status_code == 200:
                img = Image.open(BytesIO(img_resp.content))
                img = img.resize((1280, 720), Image.Resampling.LANCZOS)
                img.convert("RGB").save(output_path, "JPEG", quality=85, optimize=True)
                print(f"      ✅ Image Saved using {source_name}")
                return True
                
        except Exception as e:
            print(f"      ⚠️ {source_name} Failed: {e}")
            time.sleep(2)
    
    print("      ❌ All Image Sources Failed.")
    return False

# --- AI ENGINE ---
def parse_ai_response(text):
    try:
        parts = text.split("|||BODY_START|||")
        if len(parts) < 2: return None
        json_part = parts[0].strip()
        body_part = parts[1].strip()
        # Bersihkan markdown json jika ada
        json_part = re.sub(r'^```json', '', json_part)
        json_part = re.sub(r'```$', '', json_part)
        data = json.loads(json_part)
        data['content'] = body_part
        return data
    except Exception as e:
        print(f"      ❌ Parse Error: {e}")
        return None

def get_groq_article_seo(title, summary, link, internal_links_map, target_category):
    MODEL_NAME = "llama-3.3-70b-versatile"
    
    # Karena GTrends memberikan keyword, bukan judul lengkap, prompt harus disesuaikan
    system_prompt = f"""
    You are a Senior Trend Analyst for a major US News Outlet.
    TARGET CATEGORY: {target_category}
    
    INPUT DATA: You will receive a **Trending Keyword** and related news snippets/traffic info.
    
    TASK: Write a comprehensive news article (1000+ words) explaining WHY this keyword is trending.
    
    OUTPUT FORMAT (STRICT):
    {{"title": "Click-worthy Headline based on the Trend", "description": "SEO description (150 chars)", "category": "{target_category}", "main_keyword": "The Trending Keyword", "image_prompt": "Visual description for Flickr search (simple tags)"}}
    |||BODY_START|||
    [Markdown Article Content]

    ARTICLE STRUCTURE:
    1. **Trending Update** (Why is everyone searching this right now?)
    2. **The Full Story** (5W1H - What happened?)
    3. **Background** (Context/History)
    4. **Social Sentiment** (What people are saying)
    5. **Impact Analysis** (Why it matters)
    
    STYLE:
    - Use H2 (##) and H3 (###).
    - **Crucial**: Incorporate the keyword naturally.
    - Use internal links: {internal_links_map}.
    """

    user_prompt = f"""
    TRENDING KEYWORD: {title}
    CONTEXT/SNIPPETS: {summary}
    GOOGLE SEARCH LINK: {link}
    
    Analyze this trend and write the article.
    """

    for index, api_key in enumerate(GROQ_API_KEYS):
        try:
            print(f"      🤖 AI Writing ({target_category})...")
            client = Groq(api_key=api_key)
            completion = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=6000,
            )
            return completion.choices[0].message.content

        except BadRequestError as e:
            print(f"      ⚠️ GROQ 400 ERROR: {e.body}")
            continue
        except Exception as e:
            print(f"      ⚠️ Error (Key #{index+1}): {e}")
            continue
            
    return None

# --- MAIN LOOP ---
def main():
    os.makedirs(CONTENT_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    total_generated = 0

    # LOOPING SETIAP KATEGORI
    for category_name, rss_url in CATEGORY_URLS.items():
        print(f"\n📡 Fetching GTrends: {category_name}...")
        
        # Parse RSS
        feed = feedparser.parse(rss_url)
        
        if not feed.entries:
            print(f"   ⚠️ No trends found for {category_name}. Skipping.")
            continue

        cat_success_count = 0
        
        for entry in feed.entries:
            if cat_success_count >= TARGET_PER_CATEGORY:
                break

            # Di Google Trends RSS, entry.title adalah KEYWORD (misal: "Taylor Swift")
            trending_keyword = entry.title
            
            # entry.summary biasanya berisi snippet berita terkait atau data traffic
            trend_context = entry.summary if 'summary' in entry else "High Search Volume"
            
            clean_slug = slugify(trending_keyword)
            filename = f"{clean_slug}.md"

            if os.path.exists(f"{CONTENT_DIR}/{filename}"):
                continue

            print(f"   🔥 Trending: {trending_keyword}...")
            
            # 1. Generate AI
            context = get_internal_links_context()
            raw_response = get_groq_article_seo(trending_keyword, trend_context, entry.link, context, category_name)
            
            if not raw_response:
                print("      ❌ AI Failed.")
                continue

            data = parse_ai_response(raw_response)
            if not data:
                print("      ❌ Parse Failed.")
                continue

            # 2. Image (Multi-Source Fallback)
            img_name = f"{clean_slug}.jpg"
            # Gunakan keyword dari AI atau keyword asli untuk pencarian gambar
            image_search_term = data.get('image_prompt', trending_keyword)
            has_img = download_and_optimize_image(image_search_term, img_name)
            
            final_img = f"/images/{img_name}" if has_img else "/images/default-trend.jpg"
            
            # 3. Save Markdown
            date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-05:00")
            
            md = f"""---
title: "{data['title'].replace('"', "'")}"
date: {date}
author: "{AUTHOR_NAME}"
categories: ["{data['category']}"]
tags: ["{data['main_keyword']}", "Trending"]
featured_image: "{final_img}"
description: "{data['description'].replace('"', "'")}"
draft: false
---

{data['content']}

---
*Sources: Analysis based on Google Trends Data and Search Volume.*
"""
            with open(f"{CONTENT_DIR}/{filename}", "w", encoding="utf-8") as f: f.write(md)
            
            if 'main_keyword' in data: 
                save_link_to_memory(data['main_keyword'], clean_slug)
            
            print(f"   ✅ Published: {filename}")
            cat_success_count += 1
            total_generated += 1
            
            print("   zzz... Cooling down 10s...")
            time.sleep(10)

    print(f"\n🎉 DONE! Total articles generated: {total_generated}")

if __name__ == "__main__":
    main()
