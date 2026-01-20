import os
import json
import requests
import feedparser
import time
from datetime import datetime
from slugify import slugify
from dotenv import load_dotenv
from io import BytesIO
from PIL import Image

# CONFIG
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
RSS_URL = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
CONTENT_DIR = "content/articles"
IMAGE_DIR = "static/images"
MEMORY_FILE = "automation/data/link_memory.json"
AUTHOR_NAME = "US News Desk"

def load_memory():
    if not os.path.exists(MEMORY_FILE): return {}
    try:
        with open(MEMORY_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_memory(keyword, slug):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    mem = load_memory()
    mem[keyword.lower().strip()] = f"/articles/{slug}"
    with open(MEMORY_FILE, 'w') as f: json.dump(mem, f, indent=2)

def optimize_image(prompt, filename):
    safe_prompt = prompt.replace(" ", "%20")[:150]
    url = f"https://image.pollinations.ai/prompt/{safe_prompt}?width=1280&height=720&nologo=true&model=flux"
    try:
        print(f"🎨 Getting Image: {filename}")
        res = requests.get(url, timeout=30)
        if res.status_code == 200:
            img = Image.open(BytesIO(res.content))
            img = img.resize((1280, 720), Image.Resampling.LANCZOS)
            output = f"{IMAGE_DIR}/{filename}"
            img.convert("RGB").save(output, "JPEG", quality=75, optimize=True)
            return True
    except Exception as e:
        print(f"Image Error: {e}")
    return False

def generate_article(title, summary, link, memory_context):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    
    system = """You are a Senior US Journalist. Write a news article adhering to E-E-A-T.
    1. Identify and **Bold** key entities (People, Places, orgs).
    2. Use the provided JSON list for internal linking: If you mention a keyword, link it as `[keyword](/articles/slug)`.
    3. Include the original source link at the end.
    4. Write in objective American English."""
    
    user = f"""Source: {title} ({link})\nSnippet: {summary}\n\nMemory: {memory_context}\n\nOutput JSON: title, content (markdown), description, image_prompt, category, main_keyword."""
    
    data = {
        "model": "llama3-70b-8192",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"}
    }
    
    try:
        res = requests.post(url, headers=headers, json=data)
        return res.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"Groq Error: {e}")
        return None

def main():
    os.makedirs(CONTENT_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    
    print("📡 Checking News...")
    feed = feedparser.parse(RSS_URL)
    if not feed.entries: return
    
    entry = feed.entries[0]
    slug = slugify(entry.title.split(" - ")[0])
    filename = f"{slug}.md"
    
    if os.path.exists(f"{CONTENT_DIR}/{filename}"):
        print("Skipping duplicate.")
        return

    print(f"🔥 Processing: {entry.title}")
    
    # Context
    mem = load_memory()
    context = json.dumps(dict(list(mem.items())[-50:]))
    
    # Generate
    json_res = generate_article(entry.title, entry.summary, entry.link, context)
    if not json_res: return
    data = json.loads(json_res)
    
    # Image
    img_name = f"{slug}.jpg"
    has_img = optimize_image(data['image_prompt'], img_name)
    img_path = f"/images/{img_name}" if has_img else "/images/default-news.jpg"
    
    # Save
    date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-05:00")
    md = f"""---
title: "{data['title']}"
date: {date}
author: "{AUTHOR_NAME}"
categories: ["{data['category']}"]
tags: ["{data['main_keyword']}"]
featured_image: "{img_path}"
description: "{data['description']}"
draft: false
---
{data['content']}
---
*Source: [Original Article]({entry.link})*
"""
    with open(f"{CONTENT_DIR}/{filename}", "w", encoding="utf-8") as f: f.write(md)
    
    # Update Memory
    save_memory(data['main_keyword'], slug)
    print("✅ Done.")

if __name__ == "__main__":
    main()