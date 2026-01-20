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

# --- 1. CONFIGURATION ---
load_dotenv()

# LOGIKA MULTI-API KEY
# Mengambil string panjang dari .env atau GitHub Secret, lalu memecahnya berdasarkan koma
GROQ_KEYS_RAW = os.getenv("GROQ_API_KEY", "")
# List ini akan berisi ['key1', 'key2', 'key3']
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

# --- 2. SMART MEMORY SYSTEM (SEO LINKING) ---

def load_link_memory():
    """Membaca database artikel lama untuk strategi internal linking."""
    if not os.path.exists(MEMORY_FILE):
        return {}
    try:
        with open(MEMORY_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def save_link_to_memory(keyword, slug):
    """Menyimpan artikel baru ke database agar bisa direferensikan artikel masa depan."""
    os.makedirs(DATA_DIR, exist_ok=True)
    memory = load_link_memory()
    
    # Bersihkan keyword (huruf kecil & hapus spasi berlebih)
    clean_key = keyword.lower().strip()
    
    # Simpan format: "bitcoin price" -> "/articles/bitcoin-price-drop"
    memory[clean_key] = f"/articles/{slug}"
    
    with open(MEMORY_FILE, 'w') as f:
        json.dump(memory, f, indent=2)

def get_internal_links_context():
    """Mengambil 50 topik terakhir untuk disuapkan ke AI sebagai konteks."""
    memory = load_link_memory()
    # Ambil 50 item terakhir (terbaru) dari dictionary
    items = list(memory.items())[-50:] 
    return json.dumps(dict(items))

# --- 3. IMAGE ENGINE (COMPRESSION & RESIZE) ---

def download_and_optimize_image(prompt, filename):
    """Download gambar dari Pollinations, Resize ke 720p, dan Compress JPG."""
    
    # Bersihkan prompt agar URL valid
    safe_prompt = prompt.replace(" ", "%20")[:150]
    
    # Request ke Pollinations (Model Flux - High Quality)
    image_url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1280&height=720&nologo=true&model=flux"
    
    print(f"🎨 Generating Image: {filename}...")
    try:
        # Timeout 30 detik mencegah script hang
        response = requests.get(image_url, timeout=30)
        
        if response.status_code == 200:
            # Load gambar ke Memory (RAM)
            img = Image.open(BytesIO(response.content))
            
            # 1. Resize Wajib ke 1280x720 (Standar Google Discover)
            img = img.resize((1280, 720), Image.Resampling.LANCZOS)
            
            # 2. Simpan sebagai JPG dengan Kompresi (Quality 75)
            output_path = f"{IMAGE_DIR}/{filename}"
            img.convert("RGB").save(output_path, "JPEG", quality=75, optimize=True)
            
            print("✅ Image Optimized & Saved successfully.")
            return True
        else:
            print(f"❌ Image Download Failed. Status Code: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Image Error: {e}")
        return False

# --- 4. AI CONTENT ENGINE (MULTI-KEY SUPPORT) ---

def get_groq_article_seo(title, summary, link, internal_links_map):
    """Generate artikel menggunakan Groq API dengan sistem rotasi kunci otomatis."""
    
    url = "https://api.groq.com/openai/v1/chat/completions"
    
    # Prompt System (Instruksi Utama)
    system_prompt = """
    You are a Senior US Journalist and SEO Specialist. 
    Your goal is to write a news article that adheres to Google's E-E-A-T standards.
    
    STRICT GUIDELINES:
    1. **Entity Salience**: Identify key people, organizations, or locations and **Bold** them upon first mention (e.g., **Elon Musk**, **White House**).
    2. **Internal Linking**: I will provide a JSON list of existing articles. If you mention any keyword from that list, you MUST create a Markdown link to it. Example: `[keyword](/articles/slug)`. Link naturally within the text.
    3. **External Linking**: You MUST include the original source link provided at the end. Also, include 1 authoritative link (Wikipedia/.gov/.edu) for context if needed.
    4. **Structure**: 
       - Catchy Headline (Max 60 chars, No clickbait).
       - First Paragraph: Answer "Who, What, Why" immediately.
       - Use H2 and H3 subheadings.
    5. **Tone**: Objective, professional, American English.
    """

    # Prompt User (Data Berita)
    user_prompt = f"""
    SOURCE NEWS:
    Title: "{title}"
    Snippet: "{summary}"
    Original URL: {link}
    
    YOUR INTERNAL LINK MEMORY (Use these if relevant):
    {internal_links_map}

    TASK:
    Write a full article in Markdown.
    
    OUTPUT FORMAT (JSON ONLY):
    {{
        "title": "...",
        "content": "Full markdown text...",
        "image_prompt": "Cinematic, photorealistic, 8k, no text, description of the news context...",
        "description": "SEO Meta description (150 chars)",
        "category": "Technology/Business/Politics/Crypto/Health",
        "main_keyword": "Single most important keyword of this article"
    }}
    """
    
    data = {
        "model": "llama3-70b-8192",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.6,
        "response_format": {"type": "json_object"}
    }

    # --- LOGIKA ROTASI KUNCI (KEY ROTATION) ---
    # Loop ini akan mencoba kunci satu per satu sampai berhasil
    for index, api_key in enumerate(GROQ_API_KEYS):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        try:
            print(f"🤖 AI Writing... (Attempting with Key #{index + 1})")
            response = requests.post(url, headers=headers, json=data)
            
            # Khusus jika error 429 (Rate Limit Habis)
            if response.status_code == 429:
                print(f"⚠️ Key #{index + 1} Rate Limited! Switching to next key...")
                continue # Lanjut ke putaran loop berikutnya (kunci selanjutnya)
            
            # Cek error lain (misal server error 500)
            response.raise_for_status()
            
            # Jika berhasil, kembalikan isi konten
            return response.json()['choices'][0]['message']['content']

        except Exception as e:
            # Jika errornya bukan 429 (misal koneksi putus), print errornya
            print(f"⚠️ Error with Key #{index + 1}: {e}")
            
            # Jika ini adalah kunci terakhir dan masih gagal, hentikan proses
            if index == len(GROQ_API_KEYS) - 1:
                print("❌ ALL API KEYS FAILED. Aborting generation.")
                return None
            
            # Lanjut ke kunci berikutnya
            continue

# --- 5. MAIN EXECUTION FLOW ---

def main():
    # 1. Setup Folder (Pastikan folder ada)
    os.makedirs(CONTENT_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)

    # 2. Fetch Google News US
    print(f"📡 Fetching US News Feed...")
    feed = feedparser.parse(TARGET_CONFIG['rss_url'])
    
    if not feed.entries:
        print("📭 No news found in RSS Feed.")
        return

    # Ambil berita paling atas (Top Story)
    entry = feed.entries[0]
    
    # Bersihkan Judul & Buat Slug
    clean_title = entry.title.split(" - ")[0] # Hapus nama media
    file_slug = slugify(clean_title)
    filename = f"{file_slug}.md"

    # 3. Deduplikasi (Cek apakah berita sudah ada)
    if os.path.exists(f"{CONTENT_DIR}/{filename}"):
        print(f"⚠️ Article already exists: '{clean_title}'. Skipping to avoid duplicate.")
        return

    print(f"🔥 Processing New Article: {clean_title}")

    # 4. Siapkan Konteks Link (Memory)
    internal_links_context = get_internal_links_context()

    # 5. Generate Konten (AI)
    json_res = get_groq_article_seo(clean_title, entry.summary, entry.link, internal_links_context)
    
    if not json_res: 
        print("❌ Failed to generate content from AI.")
        return
    
    try:
        data = json.loads(json_res)
    except json.JSONDecodeError:
        print("❌ AI response was not valid JSON.")
        return

    # 6. Generate & Optimize Image
    image_filename = f"{file_slug}.jpg"
    has_image = download_and_optimize_image(data['image_prompt'], image_filename)
    
    # Gunakan gambar default jika download gagal
    final_image = f"/images/{image_filename}" if has_image else "/images/default-news.jpg"

    # 7. Tulis File Markdown (Hugo Format)
    date_now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-05:00") # Jam US
    
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

    # 8. UPDATE MEMORY (Database Injection)
    # Simpan keyword agar bisa di-link oleh artikel selanjutnya
    if 'main_keyword' in data and data['main_keyword']:
        save_link_to_memory(data['main_keyword'], file_slug)
        print(f"🧠 Memory Updated: '{data['main_keyword']}' -> '/articles/{file_slug}'")
    
    print(f"✅ SUCCESS! Article saved to: {CONTENT_DIR}/{filename}")

if __name__ == "__main__":
    main()