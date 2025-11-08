import json, sqlite3
from config import LOCALES_FILE, DB

conn = sqlite3.connect(DB, check_same_thread=False)
cursor = conn.cursor()

with open(LOCALES_FILE, "r", encoding="utf-8") as f:
    LOCALE = json.load(f)

user_lang = {}

def get_lang(chat_id):
    if chat_id in user_lang:
        return user_lang[chat_id]
    cursor.execute("SELECT lang FROM users WHERE user_id=?", (chat_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT OR IGNORE INTO users (user_id, lang) VALUES (?, 'uz')", (chat_id,))
        conn.commit()
        user_lang[chat_id] = "uz"
        return "uz"
    user_lang[chat_id] = row[0] or "uz"
    return user_lang[chat_id]

def set_lang(chat_id, lang):
    user_lang[chat_id] = lang
    cursor.execute("UPDATE users SET lang=? WHERE user_id=?", (lang, chat_id))
    conn.commit()

def L(chat_id, key, **kwargs):
    lang = get_lang(chat_id)
    text = LOCALE.get(lang, {}).get(key, key)
    if kwargs:
        try: text = text.format(**kwargs)
        except: pass
    return text
