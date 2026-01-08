import time
import re
import json
import requests
import io
import base64
import random
import xml.etree.ElementTree as ET
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from google.genai import types as genai_types
from config import client, bot, STEP_GIFS

# --- UTILITIES ---
def slugify(text):
    if not text: return "image"
    symbols = (u"абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ",
               u"abvgdeejzijklmnoprstufhzcss_y_euaABVGDEEJZIJKLMNOPRSTUFHZCSS_Y_EUA")
    tr = {ord(a): ord(b) for a, b in zip(*symbols)}
    text = text.translate(tr)
    text = re.sub(r'[\W\s]+', '-', text).strip('-').lower()
    return text

def escape_md(text):
    if not text: return ""
    return str(text).replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")

def send_step_animation(chat_id, step_key, caption=None):
    gif_url = STEP_GIFS.get(step_key)
    if gif_url:
        try:
            bot.send_animation(chat_id, gif_url, caption=caption, parse_mode='Markdown')
        except:
            if caption: bot.send_message(chat_id, caption, parse_mode='Markdown')
    elif caption:
        bot.send_message(chat_id, caption, parse_mode='Markdown')

def send_safe_message(chat_id, text, parse_mode='HTML', reply_markup=None):
    if not text: return
    MAX_LENGTH = 3800 
    parts = []
    while len(text) > 0:
        if len(text) > MAX_LENGTH:
            split_pos = text.rfind('\n', 0, MAX_LENGTH)
            if split_pos == -1: split_pos = text.rfind(' ', 0, MAX_LENGTH)
            if split_pos == -1: split_pos = MAX_LENGTH
            parts.append(text[:split_pos])
            text = text[split_pos:]
        else:
            parts.append(text)
            text = ""
    for i, part in enumerate(parts):
        is_last = (i == len(parts) - 1)
        current_markup = reply_markup if is_last else None
        try:
            bot.send_message(chat_id, part, parse_mode=parse_mode, reply_markup=current_markup)
        except Exception as e:
            try:
                clean_part = re.sub(r'<[^>]+>', '', part) 
                bot.send_message(chat_id, clean_part, parse_mode=None, reply_markup=current_markup)
            except Exception as e2: print(f"❌ Failed to send: {e2}")
        time.sleep(0.3) 

def get_gemini_response(prompt):
    try:
        response = client.models.generate_content(model="gemini-2.0-flash", contents=[prompt])
        return response.text
    except Exception as e:
        return f"AI Error: {e}"

def clean_and_parse_json(text):
    text = str(text).strip()
    match = re.search(r'```json\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try: return json.loads(match.group(1))
        except: pass
    match_list = re.search(r'```json\s*(\[.*?\])\s*```', text, re.DOTALL)
    if match_list:
        try: return json.loads(match_list.group(1))
        except: pass
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        try: return json.loads(text[start:end+1])
        except: pass
    start_list = text.find('[')
    end_list = text.rfind(']')
    if start_list != -1 and end_list != -1:
        try: return json.loads(text[start_list:end_list+1])
        except: pass
    return None

def format_html_for_chat(html_content):
    text = str(html_content).strip()
    text = re.sub(r'^```json\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    if text.startswith('{') and '"html_content":' in text:
        try:
            data = json.loads(text)
            text = data.get("html_content", text)
        except:
            match = re.search(r'"html_content":\s*"(.*?)(?<!\\)"', text, re.DOTALL)
            if match: text = match.group(1).encode('utf-8').decode('unicode_escape')
            else: text = text.replace('{', '').replace('}', '').replace('"html_content":', '')
    text = text.replace('\\n', '\n')
    text = re.sub(r'```html', '', text)
    text = re.sub(r'```', '', text)
    soup = BeautifulSoup(text, "html.parser")
    for script in soup(["script", "style", "head", "meta", "title", "link"]): script.decompose()
    for header in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        new_tag = soup.new_tag("b")
        new_tag.string = f"\n\n{header.get_text().strip()}\n"
        header.replace_with(new_tag)
    for li in soup.find_all('li'): li.string = f"• {li.get_text().strip()}\n"
    for ul in soup.find_all('ul'): ul.unwrap()
    for ol in soup.find_all('ol'): ol.unwrap()
    for p in soup.find_all('p'):
        p.append('\n\n')
        p.unwrap()
    for br in soup.find_all('br'): br.replace_with("\n")
    clean_text = str(soup)
    allowed_tags = r"b|strong|i|em|u|ins|s|strike|del|a|code|pre"
    clean_text = re.sub(r'<(?!\/?({}))[^>]*>'.format(allowed_tags), '', clean_text)
    import html
    clean_text = html.unescape(clean_text)
    clean_text = re.sub(r'\n{3,}', '\n\n', clean_text)
    return clean_text.strip()

# --- SITEMAP & PARSING UTILS ---
def parse_sitemap(url):
    links = []
    try:
        sitemap_urls = [
            url.rstrip('/') + '/sitemap.xml',
            url.rstrip('/') + '/sitemap_index.xml',
            url.rstrip('/') + '/wp-sitemap.xml'
        ]
        target_sitemap = None
        for s_url in sitemap_urls:
            try:
                r = requests.get(s_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200:
                    target_sitemap = s_url
                    break
            except: continue
        if not target_sitemap:
            return parse_html_links(url)

        def fetch_sitemap_recursive(xml_url):
            found = []
            try:
                resp = requests.get(xml_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200: return []
                root = ET.fromstring(resp.content)
                for elem in root.iter():
                    if '}' in elem.tag: elem.tag = elem.tag.split('}', 1)[1]
                for sm in root.findall('sitemap'):
                    loc = sm.find('loc')
                    if loc is not None and loc.text:
                        found.extend(fetch_sitemap_recursive(loc.text))
                for u in root.findall('url'):
                    loc = u.find('loc')
                    if loc is not None and loc.text:
                        found.append(loc.text)
            except: pass
            return found

        links = fetch_sitemap_recursive(target_sitemap)
        if not links:
            links = parse_html_links(url)
        clean_links = [l for l in list(set(links)) if not any(x in l for x in ['.jpg', '.png', '.pdf', 'wp-admin', 'feed', '.xml', 'sitemap'])]
        return clean_links
    except: return []

def parse_html_links(url):
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, 'html.parser')
        domain = urlparse(url).netloc
        local_links = []
        for a in soup.find_all('a', href=True):
            full_url = urljoin(url, a['href'])
            if urlparse(full_url).netloc == domain:
                local_links.append(full_url)
        return local_links
    except: return []

def extract_contacts_from_soup(soup):
    contacts = []
    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', soup.get_text())
    if emails: contacts.append(f"Email: {emails[0]}")
    phones = re.findall(r'(?:\+7|8)[\s-]?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}', soup.get_text())
    if phones: contacts.append(f"Phone: {phones[0]}")
    address_keywords = ["г.", "ул.", "город", "проспект", "шоссе", "Address", "Location"]
    text_blocks = soup.get_text().split('\n')
    for line in text_blocks:
        if any(x in line for x in address_keywords) and len(line) < 100 and len(line) > 10:
            contacts.append(f"Address: {line.strip()}")
            break
    return "\n".join(list(set(contacts)))

def deep_analyze_site(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
        resp = requests.get(url, timeout=15, headers=headers)
        soup = BeautifulSoup(resp.text, 'html.parser')
        title = soup.title.string if soup.title else "No Title"
        desc = soup.find("meta", attrs={"name": "description"})
        desc = desc["content"] if desc else "No Description"
        contact_info = extract_contacts_from_soup(soup)
        if "Phone" not in contact_info:
            for a in soup.find_all('a', href=True):
                href = a['href'].lower()
                if 'contact' in href or 'kontakt' in href or 'svyaz' in href:
                    try:
                        full_contact_url = urljoin(url, a['href'])
                        c_resp = requests.get(full_contact_url, timeout=10, headers=headers)
                        c_soup = BeautifulSoup(c_resp.text, 'html.parser')
                        more_contacts = extract_contacts_from_soup(c_soup)
                        contact_info += "\n" + more_contacts
                        break
                    except: pass
        raw_text = soup.get_text()[:4000].strip()
        result_text = f"URL: {url}\nTitle: {title}\nDesc: {desc}\nREAL_CONTACTS_DATA: {contact_info}\nContent: {raw_text}"
        return result_text, []
    except Exception as e: return f"Error: {e}", []

def generate_image_bytes(image_prompt, project_style="", negative_prompt=""):
    target_model = 'imagen-4.0-generate-001'
    base_negative = "exclude text, writing, letters, watermarks, signature, words, logo"
    full_negative = f"{base_negative}, {negative_prompt}" if negative_prompt else base_negative
    if project_style and len(project_style) > 5:
        final_prompt = f"{project_style}. {image_prompt}. High resolution, 8k, cinematic lighting. Exclude: {full_negative}."
    else:
        final_prompt = f"Professional photography, {image_prompt}, realistic, high resolution, 8k, cinematic lighting. Exclude: {full_negative}."
    try:
        response = client.models.generate_images(
            model=target_model, prompt=final_prompt,
            config=genai_types.GenerateImagesConfig(number_of_images=1, aspect_ratio='16:9')
        )
        if response.generated_images: return response.generated_images[0].image.image_bytes
        else: return None
    except Exception as e:
        print(f"Gen Error: {e}")
        return None