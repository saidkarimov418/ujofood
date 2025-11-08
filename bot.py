# -*- coding: utf-8 -*-
import os, json, sqlite3, math, time, random, string
import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import requests
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from geopy.distance import geodesic
import datetime

# ================= CONFIG =================
TOKEN = "8341876336:AAFn193JY7yI087v41RXdev77iEi8_B9I6k"
bot = telebot.TeleBot(TOKEN, parse_mode=None)

ADMIN_ID = [7126212094]
STORE_LAT, STORE_LON = 41.311081, 69.240562  # Tashkent center (example)
BASE_DELIVERY_FEE = 12000
FEE_PER_KM = 1500  # after 3 km
FREE_DELIVERY_THRESHOLD = 200000  # free delivery after subtotal >= this

CASHBACK_RATE = 0.05  # earn 5% points
POINT_VALUE = 1       # 1 point = 1 so'm

DB = "shop_pro.db"
PRODUCTS_FILE = "products.json"
LOCALES_FILE = "locales.json"

# ================= UTILS =================
def fmt_price(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " so'm"

def rand_code(n=8):
    return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1 = math.radians(lat1); phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlmb/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

# ================= DATA =================
conn = sqlite3.connect(DB, check_same_thread=False)
cursor = conn.cursor()

# --- Jadval migratsiyasi (agar ustun yo'q bo'lsa qo'shamiz) ---
try:
    cursor.execute("ALTER TABLE promos ADD COLUMN min_amount INTEGER DEFAULT 0;")
    conn.commit()
except sqlite3.OperationalError:
    # ustun allaqachon mavjud bo‘lsa xato chiqadi -> e'tibor bermaymiz
    pass


cursor.executescript("""
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  lang TEXT DEFAULT 'uz',
  push INTEGER DEFAULT 1,
  points INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  address TEXT,
  extra TEXT,
  status TEXT DEFAULT 'preparing',
  subtotal INTEGER,
  delivery_fee INTEGER,
  discount_text TEXT,
  total INTEGER,
  created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS order_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER,
  category TEXT,
  name TEXT,
  qty INTEGER,
  unit_price INTEGER,
  comment TEXT
);
CREATE TABLE IF NOT EXISTS ratings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER,
  user_id INTEGER,
  stars INTEGER,
  comment TEXT,
  created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS promos (
    code TEXT PRIMARY KEY,
    type TEXT,
    value INTEGER,
    extra TEXT,
    min_amount INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS daily_deal (
  id INTEGER PRIMARY KEY CHECK (id=1),
  product TEXT,
  price INTEGER
);
CREATE TABLE IF NOT EXISTS used_promos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    code TEXT
);
CREATE TABLE IF NOT EXISTS promos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE,
    type TEXT,         -- percent, fixed, bonus_item
    value INTEGER,     -- foiz yoki summa
    extra TEXT,        -- bonus mahsulot bo‘lsa
    min_amount INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS broadcasts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  text TEXT,
  sent INTEGER,
  fails INTEGER,
  created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")

conn.commit()

# make sure daily_deal row exists
cursor.execute("INSERT OR IGNORE INTO daily_deal (id, product, price) VALUES (1, '', 0)")
conn.commit()

# Load products and locales
with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
    products = json.load(f)
with open(LOCALES_FILE, "r", encoding="utf-8") as f:
    LOCALE = json.load(f)


from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from i18n import L
import json, os

PRODUCTS_FILE = "products.json"

def load_products():
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        return {}

def send_categories(chat_id):
    products = load_products()
    cats = list(products.keys())

    m = InlineKeyboardMarkup(row_width=2)
    buttons = [InlineKeyboardButton(c, callback_data=f"cat:{c}") for c in cats]

    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            m.row(buttons[i], buttons[i + 1])
        else:
            m.add(buttons[i])

    m.add(InlineKeyboardButton(L(chat_id, "back_main"), callback_data="back_main"))

    # 🟢 Matnni endi JSON dan oladi (ko‘p tilli)
    bot.send_message(
        chat_id,
        L(chat_id, "choose_category"),
        parse_mode="HTML",
        reply_markup=m
    )



# --- Mahsulotlarni saqlash ---
def save_products():
    with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=4)

# --- Boshlang‘ich yuklash ---
products = load_products()




# Runtime states
user_step = {}
user_address = {}
user_extra = {}
user_location = {}
user_lang = {}
carts = {}   # chat_id -> { "Category:Name": {"qty": int, "comment": str} }
user_promos = {}  # chat_id -> promo code string or None

# ================= I18N =================
def L(chat_id, key, **kwargs):
    lang = get_lang(chat_id)
    text = LOCALE.get(lang, {}).get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except Exception:
            pass
    return text

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

# ================= MENUS =================
def main_menu_markup(chat_id):
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton(L(chat_id, "order_now"), callback_data="order"))
    m.row(InlineKeyboardButton(L(chat_id, "about"), callback_data="about"),
          InlineKeyboardButton(L(chat_id, "my_orders"), callback_data="my_orders"))
    m.row(InlineKeyboardButton(L(chat_id, "branches"), callback_data="branches"))
    m.row(InlineKeyboardButton(L(chat_id, "feedback"), callback_data="feedback"),
          InlineKeyboardButton(L(chat_id, "settings"), callback_data="settings"))
    if chat_id in ADMIN_ID:
        m.add(InlineKeyboardButton("👑 ADMIN", callback_data="admin"))
    return m

def settings_menu(chat_id):
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton(L(chat_id, "lang"), callback_data="set_lang"))
    push_state = "ON" if get_push(chat_id) else "OFF"
    label = L(chat_id, "push_off") if push_state == "ON" else L(chat_id, "push_on")
    m.row(InlineKeyboardButton(label, callback_data="toggle_push"))
    m.add(InlineKeyboardButton(L(chat_id, "back"), callback_data="back_main"))
    return m

def admin_menu(chat_id):
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton(L(chat_id, "stats"), callback_data="adm_stats"),
        InlineKeyboardButton(L(chat_id, "top_users"), callback_data="adm_top")
    )
    m.row(
        InlineKeyboardButton(L(chat_id, "broadcast"), callback_data="adm_broadcast"),
        InlineKeyboardButton(L(chat_id, "apply_promo"), callback_data="admin_promo")
    )
    m.row(
        InlineKeyboardButton("🛠 Mahsulot qo‘shish", callback_data="admin_add"),
        InlineKeyboardButton(L(chat_id, "set_daily_deal"), callback_data="adm_daily")
    )
    m.add(InlineKeyboardButton(L(chat_id, "back"), callback_data="back_main"))
    return m


def get_push(chat_id):
    cursor.execute("SELECT push FROM users WHERE user_id=?", (chat_id,))
    row = cursor.fetchone()
    return bool(row[0]) if row else True

# ================= START =================
@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    cursor.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (chat_id,))
    conn.commit()
    bot.send_message(chat_id, L(chat_id, "main_menu"), reply_markup=main_menu_markup(chat_id))

@bot.callback_query_handler(func=lambda c: c.data == "back_main")
def back_main(call):
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(L(call.message.chat.id, "main_menu"), call.message.chat.id, call.message.message_id, reply_markup=main_menu_markup(call.message.chat.id))
    except:
        bot.send_message(call.message.chat.id, L(call.message.chat.id, "main_menu"), reply_markup=main_menu_markup(call.message.chat.id))

@bot.callback_query_handler(func=lambda call: call.data == "about")
def about_handler(call):
    chat_id = call.message.chat.id
    about_text = (
        "🍔 Ujo Food - Siz uchun sifatli fast food taomlarini yetkazib beramiz!\n\n"
        "📍 Bizning maqsadimiz - tez, mazali va sifatli taomlarni yetkazib berish.\n"
        "📞 Aloqa uchun: +998 90 123 45 67\n"
        "Bizni tanlaganingiz uchun tashakkur!"
    )
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, about_text)

@bot.callback_query_handler(func=lambda call: call.data == "feedback")
def feedback_start(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, "O'zingizni fikiringizni bildirishingiz mumkin.\n\nIltimos, fikringizni yozib yuboring:")
    user_step[chat_id] = "waiting_feedback"


@bot.message_handler(func=lambda message: user_step.get(message.chat.id) == "waiting_feedback")
def handle_feedback(message):
    chat_id = message.chat.id
    text = message.text

    # Fikringizni kanalga yuboramiz
    try:
        bot.send_message("@UjoFood_fikr", f"Foydalanuvchi fikri\n\n{text}")
    except Exception as e:
        print("Kanalga yuborishda xatolik:", e)

    bot.send_message(chat_id, "Fikringiz uchun rahmat! Yana biror narsa kerak bo‘lsa, buyurtma menyusidan tanlang.")

    # Foydalanuvchi holatini tozalaymiz
    user_step.pop(chat_id, None)


# ================= ORDER FLOW =================
@bot.callback_query_handler(func=lambda c: c.data == "order")
def order_flow(call):
    bot.answer_callback_query(call.id)
    m = ReplyKeyboardMarkup(resize_keyboard=True)
    m.add(L(call.message.chat.id, "pickup"), L(call.message.chat.id, "delivery"))
    m.add(L(call.message.chat.id, "back_main"))
    bot.send_message(call.message.chat.id, L(call.message.chat.id, "choose_delivery"), reply_markup=m)

@bot.message_handler(func=lambda m: m.text in ["🏃 Borib olish", "🚚 Eltib berish", "🏃 Самовывоз", "🚚 Доставка"])
def choose_type(message):
    chat_id = message.chat.id
    t = message.text

    # temp_data ichida bo'sh dict bo'lsa ham tayyorlab olish
    if chat_id not in temp_data:
        temp_data[chat_id] = {}

    if t in ["🚚 Eltib berish", "🚚 Доставка"]:
        temp_data[chat_id]["order_type"] = "delivery"   # ✅ SAQLAB QO'YAMIZ
        user_step[chat_id] = "waiting_location"
        m = ReplyKeyboardMarkup(resize_keyboard=True)
        m.add(KeyboardButton(L(chat_id, "send_location"), request_location=True))
        m.add(KeyboardButton(L(chat_id, "back_main")))
        bot.send_message(chat_id, L(chat_id, "send_location"), reply_markup=m)

    else:  # pickup
        temp_data[chat_id]["order_type"] = "pickup"     # ✅ SAQLAB QO'YAMIZ
        user_step[chat_id] = "pickup_wait_location"
        m = ReplyKeyboardMarkup(resize_keyboard=True)
        m.add(KeyboardButton(L(chat_id, "sendd_location"), request_location=True))
        m.add(KeyboardButton(L(chat_id, "back_main")))
        bot.send_message(chat_id, L(chat_id, "pickup_location_request"), reply_markup=m)


user_step = {}  # foydalanuvchi bosqichi
temp_data = {}  # vaqtincha ma'lumotlar

@bot.callback_query_handler(func=lambda c: c.data == "pickup")
def pickup_start(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    user_step[chat_id] = "pickup_wait_location"
    bot.send_message(chat_id, L(chat_id, "pickup_location_request"))



@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "pickup_confirm")
def pickup_confirm(message):
    chat_id = message.chat.id
    if message.text == L(chat_id, "pickup_no"):
        user_step[chat_id] = None
        bot.send_message(chat_id, L(chat_id, "back_main"), reply_markup=main_menu_markup(chat_id))
    elif message.text == L(chat_id, "pickup_yes"):
        user_step[chat_id] = "pickup_name"
        bot.send_message(chat_id, L(chat_id, "pickup_name_request"))



@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "pickup_name")
def pickup_name(message):
    chat_id = message.chat.id
    name = message.text.strip()
    temp_data[chat_id]["name"] = name
    user_step[chat_id] = None

    # 🔹 Endi bitta xabar va faqat bitta inline keyboard
    bot.send_message(
        chat_id,
        L(chat_id, "choose_category"),
        parse_mode="HTML",
        reply_markup=get_categories_markup(chat_id)
    )

def get_categories_markup(chat_id):
    products = load_products()
    cats = list(products.keys())

    m = InlineKeyboardMarkup(row_width=2)
    buttons = [InlineKeyboardButton(c, callback_data=f"cat:{c}") for c in cats]

    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            m.row(buttons[i], buttons[i + 1])
        else:
            m.add(buttons[i])

    m.add(InlineKeyboardButton(L(chat_id, "back_main"), callback_data="back_main"))
    return m


@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "confirm_order")
def confirm_order(message):
    chat_id = message.chat.id

    # foydalanuvchi savatchasidan mahsulotlar
    order_items = send_cart(chat_id)
    if not order_items:
        bot.send_message(chat_id, L(chat_id, "cart_empty"), reply_markup=main_menu_markup(chat_id))
        return

    # savatdagi mahsulotlarni textga aylantiramiz
    order_items_text = "\n".join([f"- {x['name']} x{x['qty']}" for x in order_items])

    # buyurtma turini olamiz (delivery yoki pickup)
    order_type = temp_data[chat_id].get("order_type", "delivery")

    # yangi buyurtmani DB ga qo‘shamiz
    cursor.execute("INSERT INTO orders (user_id, type, items, status) VALUES (?, ?, ?, ?)",
                   (chat_id, order_type, order_items_text, "new"))
    conn.commit()
    order_id = cursor.lastrowid

    if order_type == "pickup":
        # Foydalanuvchidan saqlangan filial va ismni olamiz
        br = temp_data[chat_id]["branch"]["name"]
        name = temp_data[chat_id]["name"]

        # Adminga xabar
        txt = L(chat_id, "pickup_admin_notice").format(
            name=name,
            branch=br,
            items=order_items_text
        )

        m = InlineKeyboardMarkup()
        m.add(
            InlineKeyboardButton(L(chat_id, "pickup_waiting"), callback_data=f"order_wait:{order_id}"),
            InlineKeyboardButton(L(chat_id, "pickup_done"), callback_data=f"order_done:{order_id}")
        )

        for admin in ADMIN_ID:
            bot.send_message(admin, txt, reply_markup=m, parse_mode="HTML")

        # Foydalanuvchiga tasdiq
        bot.send_message(chat_id, L(chat_id, "pickup_accepted"), reply_markup=main_menu_markup(chat_id))

    else:
        # 🚚 Delivery (eltib berish) jarayoni allaqachon yozilgan bo‘ladi
        # faqat delivery uchun alohida matn yuboriladi
        txt = L(chat_id, "order_received")
        bot.send_message(chat_id, txt, reply_markup=main_menu_markup(chat_id))


@bot.message_handler(func=lambda m: m.text in ["🔙 Asosiy menyu", "🔙 Главное меню"])
def back_main_btn(message):
    bot.send_message(message.chat.id, L(message.chat.id, "main_menu"), reply_markup=main_menu_markup(message.chat.id))



@bot.callback_query_handler(func=lambda c: c.data in ["pickup_yes", "pickup_no"])
def pickup_confirm_inline(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)

    # ✅ Tugmalarni o‘chiramiz (yo‘qoladi)
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except:
        pass

    if call.data == "pickup_no":
        # foydalanuvchi qaytadan lokatsiya yuboradi
        user_step[chat_id] = "pickup_wait_location"
        m = ReplyKeyboardMarkup(resize_keyboard=True)
        m.add(KeyboardButton(L(chat_id, "send_location"), request_location=True))
        m.add(L(chat_id, "back_main"))
        bot.send_message(chat_id, L(chat_id, "pickup_location_request"), reply_markup=m)
    else:
        # foydalanuvchi ism kiritadi
        user_step[chat_id] = "pickup_name"
        bot.send_message(chat_id, L(chat_id, "pickup_name_request"))

@bot.message_handler(content_types=['location'])
def handle_location(message):
    chat_id = message.chat.id
    step = user_step.get(chat_id)

    lat, lon = message.location.latitude, message.location.longitude

    # 🔹 Keyboardni shu zahoti yopamiz
    remove_kb = types.ReplyKeyboardRemove()

    # 🚚 Eltib berish (delivery)
    if step == "waiting_location":
        user_location[chat_id] = (lat, lon)

        lat, lon = message.location.latitude, message.location.longitude

        try:
            r = requests.get(
                f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json&accept-language={get_lang(chat_id)}",
                headers={"User-Agent": "UJOFoodBot/1.0"},
                timeout=10
            )
            address = r.json().get("display_name", "—") if r.status_code == 200 else "—"
        except Exception:
            address = "—"

        user_address[chat_id] = address
        user_step[chat_id] = "pickup_confirm"

        # ✅ Inline tugmalar
        m = types.InlineKeyboardMarkup()
        m.add(
            types.InlineKeyboardButton(L(chat_id, "yes"), callback_data="cf_addr_yes"),
            types.InlineKeyboardButton(L(chat_id, "no"), callback_data="cf_addr_no")
        )

        # ✅ Bitta xabar yuboramiz va eski keyboardni olib tashlaymiz
        bot.send_message(
            chat_id,
            L(chat_id, "confirm_address").format(address=address),
            reply_markup=m
        )
        bot.send_message(chat_id, text="", reply_markup=remove_kb)  # keyboardni yopish

    # 🏃 Borib olish (pickup)
    elif step == "pickup_wait_location":
        # eng yaqin filialni topamiz
        nearest = None
        min_dist = float('inf')
        for br in BRANCHES:
            dist = geodesic((lat, lon), (br['lat'], br['lon'])).km
            if dist < min_dist:
                min_dist = dist
                nearest = br

        if not nearest:
            bot.send_message(chat_id, "❌ Filial topilmadi.")
            return

        temp_data[chat_id] = {"branch": nearest, "order_type": "pickup"}
        user_step[chat_id] = "pickup_confirm"

        text = L(chat_id, "pickup_confirm_address").format(address=nearest["address"])

        # ✅ Inline tugmalar (Ha / Yo‘q)
        m = types.InlineKeyboardMarkup()
        m.row(
            types.InlineKeyboardButton(L(chat_id, "pickup_yes"), callback_data="pickup_yes"),
            types.InlineKeyboardButton(L(chat_id, "pickup_no"), callback_data="pickup_no")
        )

        # ✅ Bitta xabar yuboramiz va eski keyboardni olib tashlaymiz
        bot.send_message(chat_id, text, reply_markup=m)
        bot.send_message(chat_id, text="", reply_markup=remove_kb)


import math
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# 📍 Do'kon manzili (filial koordinatalari)
STORE_LAT, STORE_LON = 41.109429, 69.064383
BASE_DELIVERY_FEE = 12000   # minimal
FEE_PER_KM = 1000          # 1 km uchun
FREE_DELIVERY_THRESHOLD = 200000  # masalan, 200k dan yuqori bepul yetkazib berish

# Lokatsiyalar
user_location = {}   # chat_id -> (lat, lon)

# --- Haversine formula (km hisoblash)
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# --- Savatni chiqarish ---

# 💳 To‘lov uchun vaqtinchalik saqlanadigan ma’lumotlar
user_expected_price = {}       # foydalanuvchidan kutilayotgan summa
user_payment_deadline = {}     # to‘lov qilish muddati
user_pending_payment_message = {}
user_request_time = {}
from datetime import datetime, timedelta, timezone
import pytz

# O‘zbekiston vaqt zonasi
UZBEK_TZ = pytz.timezone("Asia/Tashkent")

# ================= CART ROUTER =================



# ================== GLOBAL LUG‘ATLAR ==================
carts = {}
user_promos = {}
user_step = {}
temp_data = {}
user_location = {}
user_address = {}
user_expected_price = {}
user_payment_deadline = {}
from datetime import datetime, timedelta, timezone
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from math import radians, sin, cos, sqrt, atan2

# O'zingizdagi global o'zgaruvchilarni va funksiyalarni albatta qo'shing
# carts, temp_data, user_location, user_promos, user_step, user_expected_price, user_payment_deadline, products, L(), fmt_price(), haversine()

STORE_LAT = 41.109618858186785
STORE_LON = 69.06439327376034
FEE_PER_KM = 1000  # so'm/km


def send_cart(chat_id, msg_id=None):
    cart = carts.get(chat_id, {})
    if not cart:
        user_promos[chat_id] = None
        m = InlineKeyboardMarkup()
        m.add(InlineKeyboardButton(L(chat_id, "back"), callback_data="back_main"))
        if msg_id:
            try:
                bot.edit_message_text(L(chat_id, "cart_empty"), chat_id, msg_id, reply_markup=m)
            except:
                bot.send_message(chat_id, L(chat_id, "cart_empty"), reply_markup=m)
        else:
            bot.send_message(chat_id, L(chat_id, "cart_empty"), reply_markup=m)
        return

    order_type = temp_data.get(chat_id, {}).get("order_type")
    if not order_type:
        m = InlineKeyboardMarkup()
        m.add(InlineKeyboardButton(L(chat_id, "pickup"), callback_data="pickup"))
        m.add(InlineKeyboardButton(L(chat_id, "delivery"), callback_data="delivery"))
        bot.send_message(chat_id, "❗ Avval 'Borib olish' yoki 'Eltib berish' turini tanlang", reply_markup=m)
        return

    # --- Hisoblash ---
    lines, subtotal = [], 0
    pizza_count = 0
    for pid, info in cart.items():
        cat, name = pid.split(":", 1)
        prod = next((p for p in products.get(cat, []) if p["name"] == name), None)
        if not prod:
            continue
        qty = info["qty"]
        comment = info.get("comment", "")
        item_total = prod["price"] * qty
        subtotal += item_total
        if "pitsa" in cat.lower() or "pitsalar" in cat.lower() or "pizza" in name.lower():
            pizza_count += qty
        cline = f"🍴 {name}\n  {qty} × {fmt_price(prod['price'])} = {fmt_price(item_total)}"
        if comment:
            cline += f"\n  " + L(chat_id, "comment_label", cmt=comment)
        lines.append(cline)

    # --- Chegirmalar ---
    discount_texts, discount_amount = [], 0
    if subtotal >= 100000:
        disc = int(subtotal * 0.10)
        discount_amount += disc
        discount_texts.append("10% (100k+)")

    pcode = user_promos.get(chat_id)
    if pcode:
        cursor.execute("SELECT type, value, extra, min_amount FROM promos WHERE code=? AND active=1", (pcode,))
        row = cursor.fetchone()
        if row:
            ptype, val, extra, min_amount = row
            if subtotal >= min_amount:
                if ptype == "percent":
                    d = int(subtotal * (val / 100.0))
                    discount_amount += d
                    discount_texts.append(f"Promo {val}%")
                elif ptype == "fixed":
                    discount_amount += val
                    discount_texts.append(f"Promo {fmt_price(val)}")
                elif ptype == "bonus_item" and extra:
                    try:
                        catx, namex = extra.split(":", 1)
                        lines.append(L(chat_id, "bonus_added", name=namex))
                    except:
                        pass
            else:
                user_promos[chat_id] = None
        else:
            user_promos[chat_id] = None

    if pizza_count >= 2:
        lines.append(L(chat_id, "bonus_added", name="Cola 0.5L"))

    # --- Yetkazib berish ---
    delivery_fee, distance_km = 0, None
    if order_type == "delivery":
        if chat_id in user_location:
            lat, lon = user_location[chat_id]
            distance_km = round(haversine(STORE_LAT, STORE_LON, lat, lon), 1)
            delivery_fee = int(distance_km * FEE_PER_KM)
        else:
            bot.send_message(chat_id, "📍 Avval manzilingizni yuboring.")
            return

    # delivery_fee ni saqlaymiz
    if chat_id not in temp_data:
        temp_data[chat_id] = {}
    temp_data[chat_id]["last_delivery_fee"] = delivery_fee

    discounted = max(0, subtotal - discount_amount)
    total = discounted + delivery_fee

    # --- Matn tayyorlash ---
    text = L(chat_id, "cart_title") + "\n\n" + "\n".join(lines) + "\n"
    if discount_texts:
        text += "\n" + L(chat_id, "discount_applied", txt=", ".join(discount_texts))
    if order_type == "delivery":
        if distance_km is not None:
            text += f"\nMasofa: {distance_km} km, yetkazib berish: {fmt_price(delivery_fee)}"
        else:
            text += f"\nYetkazib berish: {fmt_price(delivery_fee)}"
    text += f"\n\n💵 Umumiy summa: {fmt_price(total)}"

    # --- Tugmalar ---
    m = InlineKeyboardMarkup()
    for pid, info in cart.items():
        name = pid.split(":", 1)[1]
        m.row(
            InlineKeyboardButton("➖", callback_data=f"cart-:{pid}"),
            InlineKeyboardButton(name, callback_data="noop"),
            InlineKeyboardButton("➕", callback_data=f"cart+:{pid}")
        )
    m.row(
        InlineKeyboardButton(L(chat_id, "order_btn"), callback_data="cart:order"),
        InlineKeyboardButton(L(chat_id, "clear_btn"), callback_data="cart:clear")
    )
    m.add(InlineKeyboardButton(L(chat_id, "apply_promo"), callback_data="promo:ask"))

    if msg_id:
        try:
            bot.edit_message_text(text, chat_id, msg_id, reply_markup=m, parse_mode="HTML")
        except:
            bot.send_message(chat_id, text, reply_markup=m, parse_mode="HTML")
    else:
        bot.send_message(chat_id, text, reply_markup=m, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data.startswith("cart"))
def cart_router(call):
    chat_id = call.message.chat.id
    data = call.data

    if data.startswith("cart-:"):
        pid = data.split(":", 1)[1]
        if pid in carts.get(chat_id, {}):
            carts[chat_id][pid]["qty"] -= 1
            if carts[chat_id][pid]["qty"] <= 0:
                del carts[chat_id][pid]
        send_cart(chat_id, call.message.message_id)
        return

    if data.startswith("cart+:"):
        pid = data.split(":", 1)[1]
        if pid in carts.get(chat_id, {}):
            carts[chat_id][pid]["qty"] += 1
        send_cart(chat_id, call.message.message_id)
        return

    if data == "cart:clear":
        carts[chat_id] = {}
        send_cart(chat_id, call.message.message_id)
        return

    if data == "promo:ask":
        user_step[chat_id] = "promo_wait"
        bot.send_message(chat_id, L(chat_id, "promo_enter"))
        return

    if data == "cart:order":
        order_type = temp_data.get(chat_id, {}).get("order_type")
        if not order_type:
            bot.send_message(chat_id, "❌ Avval yetkazib berish yoki olib ketishni tanlang.")
            return

        cart = carts.get(chat_id, {})
        if not cart:
            bot.send_message(chat_id, "❗ Savatchangiz bo‘sh.")
            return

        subtotal = sum(
            next(
                (p["price"] for p in products.get(pid.split(":", 1)[0], [])
                 if p["name"] == pid.split(":", 1)[1]),
                0
            ) * info["qty"]
            for pid, info in cart.items()
        )

        if order_type == "pickup":
            delivery_fee = 0
        else:
            delivery_fee = temp_data.get(chat_id, {}).get("last_delivery_fee", 0)

        total = subtotal + delivery_fee

        user_expected_price[chat_id] = total
        user_payment_deadline[chat_id] = datetime.now(timezone(timedelta(hours=5))) + timedelta(minutes=5)

        bot.send_message(
            chat_id,
            f"✅ Buyurtmangiz qabul qilindi.\n\n"
            f"💰 To‘lov summasi: {fmt_price(total)}\n"
            f"💳 Karta raqami: 9860 1606 1532 9804\n"
            f"👩 Qabul qiluvchi: Gulnoza Baymirzayeva yoki Gulnoza Boymirzayeva\n\n"
            f"❗ To‘lovni amalga oshirib, chekni shu yerga yuboring.\n"
            f"⏳ Muddat: 5 daqiqa ichida"
        )

        if order_type == "delivery" and chat_id not in user_location:
            user_step[chat_id] = "waiting_location"
            bot.send_message(
                chat_id,
                "📍 Iltimos, manzilingizni yuboring.",
                reply_markup=types.ReplyKeyboardMarkup(
                    resize_keyboard=True, one_time_keyboard=True
                ).add(types.KeyboardButton("📍 Manzilni yuborish", request_location=True))
            )
        return

# ================== CHEK QABUL QILISH ==================

import pytesseract
import re
import os
from datetime import datetime, timedelta, timezone
from PIL import Image

# ✅ Tesseract yo‘lini avtomatik tanlash
if os.name == "nt":  # Windows
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
else:  # Linux (Render server)
    pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"

# --- Rasm orqali chek qabul qilish ---
@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    chat_id = message.chat.id

    # --- 1️⃣ Agar foydalanuvchi chek yuborayotgan bo'lsa ---
    if chat_id in user_expected_price:
        expected = user_expected_price[chat_id]
        deadline = user_payment_deadline.get(chat_id)
        if deadline and datetime.now(timezone(timedelta(hours=5))) > deadline:
            bot.send_message(chat_id, "❌ To‘lov muddati tugagan.")
            return

        # 📥 Rasmni saqlash
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded = bot.download_file(file_info.file_path)
        path = f"receipt_{chat_id}.png"
        with open(path, "wb") as f:
            f.write(downloaded)

        # 🔎 OCR orqali matnni o‘qish (pytesseract)
        text = pytesseract.image_to_string(Image.open(path), lang="eng+rus+uzb").lower()

        # --- Karta raqami ---
        if "9860" not in text or "9804" not in text:
            bot.send_message(chat_id, "❌ Chekdagi karta raqami mos kelmadi.")
            return

        # --- Qabul qiluvchi ismi ---
        if "gulnoza" not in text:
            bot.send_message(chat_id, "❌ Qabul qiluvchi ismi mos kelmadi.")
            return

        # --- Sana tekshirish ---
        today = datetime.now(timezone(timedelta(hours=5))).strftime("%d.%m.%Y")
        found_dates = re.findall(r"(\d{2}[./-]\d{2}[./-]\d{4})", text)

        if found_dates:
            receipt_date = found_dates[0].replace("/", ".").replace("-", ".")
            if receipt_date != today:
                bot.send_message(chat_id, f"❌ Chek sanasi mos emas. ({receipt_date} o‘rniga {today} bo‘lishi kerak)")
                return
        else:
            bot.send_message(chat_id, "❌ Chekdan sana topilmadi.")
            return

        # --- Summani tekshirish ---
        found = re.findall(r"\d[\d\s]*", text)
        numbers = [int(x.replace(" ", "")) for x in found if x.strip().isdigit()]
        if not numbers:
            bot.send_message(chat_id, "❌ Chekdan summa topilmadi.")
            return

        paid = max(numbers)  # eng katta sonni olamiz
        if paid < expected:
            bot.send_message(chat_id, f"❌ Chekdagi summa ({fmt_price(paid)}) kerakli summadan kam.")
        else:
            bot.send_message(chat_id, f"✅ Chekingiz tasdiqlandi! ({fmt_price(paid)} to‘langan)")

        return  # chek handlerini yakunlaymiz

    # --- 2️⃣ Agar admin mahsulot qo‘shayotgan bo'lsa ---
    if chat_id in adding_product and adding_product[chat_id]["step"] == "get_photo":
        adding_product[chat_id]["photo"] = message.photo[-1].file_id
        adding_product[chat_id]["step"] = "get_name"
        bot.send_message(chat_id, "✏️ Mahsulot nomini yuboring:")
        return

    # Agar foydalanuvchi aktiv buyurtma yoki product qo‘shish stepida bo‘lmasa
    bot.send_message(chat_id, "❌ Sizda aktiv buyurtma yo‘q yoki rasm yuborish kutilmayapti.")

import re

def load_products():
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Normalize prices to integers
        for cat, items in data.items():
            for p in items:
                raw = str(p.get("price", 0))
                digits = re.sub(r"[^\d]", "", raw) or "0"
                p["price"] = int(digits)
        return data
    else:
        return {}

@bot.message_handler(func=lambda m: isinstance(user_step.get(m.chat.id,''), str) and user_step.get(m.chat.id,'').startswith("comment:"))
def save_comment(message):
    chat_id = message.chat.id
    key = user_step[chat_id].split(":",1)[1]
    # ensure cart entry exists
    cart = carts.setdefault(chat_id, {})
    if key not in cart:
        cart[key] = {"qty": 1, "comment": ""}
    # set comment only if user didn't send "—"
    if message.text.strip() != "—":
        cart[key]["comment"] = message.text.strip()
        bot.send_message(chat_id, L(chat_id, "comment_saved"))
    user_step[chat_id] = "choose_category"
    send_cart(chat_id)


# ================== DELIVERY ORDER ==================
def send_delivery_order(chat_id):
    cart = carts.get(chat_id, {})
    if not cart:
        bot.send_message(chat_id, "❗ Savatchangiz bo‘sh.")
        return

    # --- Mahsulotlar va subtotal ---
    lines, subtotal, pizza_count = [], 0, 0
    for pid, info in cart.items():
        cat, name = pid.split(":", 1)
        prod = next((p for p in products.get(cat, []) if p["name"] == name), None)
        if not prod:
            continue
        qty = info["qty"]
        item_total = prod["price"] * qty
        subtotal += item_total
        if "pitsa" in cat.lower() or "pitsalar" in cat.lower() or "pizza" in name.lower():
            pizza_count += qty
        lines.append(f"{qty} × {name} — {fmt_price(item_total)}")

    items_text = "\n".join(lines)

    # --- Chegirmalar ---
    discount_texts, discount_amount = [], 0
    if subtotal >= 100000:
        disc = int(subtotal * 0.10)
        discount_amount += disc
        discount_texts.append("10% (100k+)")

    # Promo kod
    pcode = user_promos.get(chat_id)
    if pcode:
        cursor.execute("SELECT type, value, extra, min_amount FROM promos WHERE code=? AND active=1", (pcode,))
        row = cursor.fetchone()
        if row:
            ptype, val, extra, min_amount = row
            if subtotal >= min_amount:
                if ptype == "percent":
                    d = int(subtotal * (val / 100.0))
                    discount_amount += d
                    discount_texts.append(f"Promo {val}%")
                elif ptype == "fixed":
                    discount_amount += val
                    discount_texts.append(f"Promo {fmt_price(val)}")

    # --- Yetkazib berish narxi ---
    address = user_address.get(chat_id, "—")
    delivery_fee, distance_km = 0, None
    if chat_id in user_location:
        lat, lon = user_location[chat_id]
        distance_km = round(haversine(STORE_LAT, STORE_LON, lat, lon), 1)
        delivery_fee = int(distance_km * FEE_PER_KM)
        if subtotal - discount_amount >= FREE_DELIVERY_THRESHOLD:
            delivery_fee = 0

    # --- Yakuniy hisob ---
    discounted = max(0, subtotal - discount_amount)
    total = discounted + delivery_fee

    # --- Adminlarga yuborish ---
    send_order_to_admin(
        order_id="DELIV-" + str(chat_id),   # kerak bo‘lsa DB dan ID olasiz
        chat_id=chat_id,
        order_type="delivery",
        items_text=items_text,
        subtotal=subtotal,
        total=total,
        discount_amount=discount_amount,
        discount_texts=discount_texts,
        address=address,
        extra=None,
        name=None,
        branch=None,
        delivery_fee=delivery_fee
    )

    # --- Foydalanuvchiga tasdiq ---
    # --- Foydalanuvchiga tasdiq ---
    msg = L(chat_id, "order_confirmed")  # tilga qarab: uz/ru
    bot.send_message(chat_id, msg)

    carts[chat_id] = {}
    user_promos.pop(chat_id, None)


# ================== PICKUP ORDER ==================

def send_pickup_order(chat_id):
    cart = carts.get(chat_id, {})
    if not cart:
        bot.send_message(chat_id, "❗ Savatchangiz bo‘sh.")
        return

    # --- Mahsulotlar va subtotal ---
    lines, subtotal, pizza_count = [], 0, 0
    for pid, info in cart.items():
        cat, name = pid.split(":", 1)
        prod = next((p for p in products.get(cat, []) if p["name"] == name), None)
        if not prod:
            continue
        qty = info["qty"]
        item_total = prod["price"] * qty
        subtotal += item_total
        if "pitsa" in cat.lower() or "pitsalar" in cat.lower() or "pizza" in name.lower():
            pizza_count += qty
        lines.append(f"{qty} × {name} — {fmt_price(item_total)}")

    items_text = "\n".join(lines)

    # --- Chegirmalar ---
    discount_texts, discount_amount = [], 0
    if subtotal >= 100000:
        disc = int(subtotal * 0.10)
        discount_amount += disc
        discount_texts.append("10% (100k+)")

    # Promo kod
    pcode = user_promos.get(chat_id)
    if pcode:
        cursor.execute("SELECT type, value, extra, min_amount FROM promos WHERE code=? AND active=1", (pcode,))
        row = cursor.fetchone()
        if row:
            ptype, val, extra, min_amount = row
            if subtotal >= min_amount:
                if ptype == "percent":
                    d = int(subtotal * (val / 100.0))
                    discount_amount += d
                    discount_texts.append(f"Promo {val}%")
                elif ptype == "fixed":
                    discount_amount += val
                    discount_texts.append(f"Promo {fmt_price(val)}")

    # --- Yakuniy hisob ---
    discounted = max(0, subtotal - discount_amount)
    total = discounted

    # --- Mijoz ma'lumotlari ---
    name = temp_data.get(chat_id, {}).get("name", "—")
    branch = temp_data.get(chat_id, {}).get("branch", {}).get("name", "—")

    # --- Adminlarga yuborish ---
    send_order_to_admin(
        order_id="PICKUP-" + str(chat_id),   # kerak bo‘lsa DB dan ID olasiz
        chat_id=chat_id,
        order_type="pickup",
        items_text=items_text,
        subtotal=subtotal,
        total=total,
        discount_amount=discount_amount,
        discount_texts=discount_texts,
        address=None,
        extra=None,
        name=name,
        branch=branch,
        delivery_fee=0
    )

    # --- Foydalanuvchiga tasdiq ---
    msg = L(chat_id, "order_confirmed")  # tilga qarab: uz/ru
    bot.send_message(chat_id, msg)

    carts[chat_id] = {}
    user_promos.pop(chat_id, None)


# ================= ORDER STATUS HANDLER =================
@bot.callback_query_handler(func=lambda call: call.data.startswith("ostatus:"))
def order_status_handler(call):
    # format: ostatus:order_id:status:user_id
    data = call.data.split(":")
    if len(data) < 4:
        return

    order_id = data[1]
    status = data[2]
    user_id = int(data[3])

    if status == "wait":
        msg = L(user_id, "order_status_wait")
        bot.send_message(user_id, f"{L(user_id, 'status_updatedD').format(status=msg)}")
        bot.answer_callback_query(call.id, "✅ Mijozga 'Jarayonda' xabari yuborildi.")

    elif status == "ready":
        order_type = temp_data.get(user_id, {}).get("order_type")
        if order_type == "pickup":
            msg = L(user_id, "order_status_ready")
        else:
            msg = "🚚 Yetkazilmoqda" if get_lang(user_id) == "uz" else "🚚 Доставляется"

        bot.send_message(user_id, f"{L(user_id, 'status_updatedD').format(status=msg)}")
        bot.answer_callback_query(call.id, "✅ Mijozga 'Tayyor' xabari yuborildi.")

    elif status == "done":
        order_type = temp_data.get(user_id, {}).get("order_type")
        if order_type == "pickup":
            msg = L(user_id, "order_status_done_pickup")
        else:
            msg = L(user_id, "order_status_done_delivery")

        bot.send_message(user_id, f"{L(user_id,'status_updatedD').format(status=msg)}")
        bot.answer_callback_query(call.id, "✅ Mijozga yakuniy xabar yuborildi.")


# === Adminlarga buyurtma yuborish ===
def send_order_to_admin(order_id, chat_id, order_type, items_text, subtotal, total, discount_amount=0, discount_texts=None, address=None, extra=None, name=None, branch=None, delivery_fee=0):
    discount_texts = discount_texts or []

    # Chegirma qismi
    if discount_amount > 0:
        discount_info = (
            f"\n💸 Chegirma: {', '.join(discount_texts)}"
            f"\n❌ Chegirmasiz: {fmt_price(subtotal)}"
            f"\n✅ To‘lanadi: {fmt_price(total)}"
        )
    else:
        discount_info = f"\n💵 Jami: {fmt_price(total)}"

    if order_type == "delivery":
        text = (
            f"🆕 🚚 Eltib berish buyurtma\n\n"
            f"👤 Foydalanuvchi ID: {chat_id}\n"
            f"🛒 Mahsulotlar:\n{items_text}\n"
            f"{discount_info}"
            f"\n🚚 Yetkazib berish narxi: {fmt_price(delivery_fee)}\n"
            f"📍 Manzil: {address or '-'}\n"
            f"➕ Qo‘shimcha: {extra or '-'}"
        )
        done_label = "✅ Yetkazildi"
    else:
        text = (
            f"🆕 🏃 Borib olish buyurtma\n\n"
            f"👤 Mijoz: {name or '-'}\n"
            f"🏢 Filial: {branch or '-'}\n"
            f"🛒 Mahsulotlar:\n{items_text}\n"
            f"{discount_info}"
        )
        done_label = "✅ Olib ketildi"

    # Tugmalar
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("⏳ Jarayonda", callback_data=f"ostatus:{order_id}:wait:{chat_id}"),
        InlineKeyboardButton("🍽 Tayyor", callback_data=f"ostatus:{order_id}:ready:{chat_id}")
    )
    m.row(
        InlineKeyboardButton(done_label, callback_data=f"ostatus:{order_id}:done:{chat_id}")
    )

    # Adminlarga yuborish
    for aid in ADMIN_ID:
        try:
            bot.send_message(aid, text, reply_markup=m)
        except Exception as e:
            print(f"Admin {aid} ga yuborilmadi: {e}")



# --- Admin tugmalarini ishlash ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("ostatus:"))
def change_order_status(call):
    _, order_id, status, user_id = call.data.split(":")
    user_id = int(user_id)

    if status == "wait":
        st_text = L(user_id, "order_status_wait")
    elif status == "ready":
        st_text = L(user_id, "order_status_ready")
    else:  # done
        # delivery yoki pickup farqi yo‘q – foydalanuvchiga ko‘rsatiladi
        st_text = L(user_id, "status_updated", status=L(user_id, "order_status_done_delivery"))

    # Foydalanuvchiga xabar
    try:
        bot.send_message(user_id, L(user_id, "status_updated", status=st_text))
    except:
        pass

    # Admin xabarini yangilash
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except:
        pass

    bot.answer_callback_query(call.id, "✅ Yangilandi")



@bot.callback_query_handler(func=lambda c: c.data in ["cf_addr_yes", "cf_addr_no"])
def confirm_addr(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)

    # ✅ Tugmalarni o‘chirish (ya'ni yo‘qoladi)
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except:
        pass

    if call.data == "cf_addr_yes":
        user_step[chat_id] = "extra_info"
        bot.send_message(chat_id, "✍️ Qo‘shimcha izoh kiriting (masalan: 4-qavat, eshik 15)")
    else:
        user_step[chat_id] = "waiting_location"
        bot.send_message(chat_id, "📍 Iltimos, manzilingizni qayta yuboring.")


@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "extra_info")
def save_extra(message):
    user_extra[message.chat.id] = message.text.strip()
    user_step[message.chat.id] = "choose_category"
    send_categories(message.chat.id)

# ================= CATEGORIES & PRODUCTS =================

@bot.callback_query_handler(func=lambda c: c.data.startswith("cat:"))
def open_category(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    cat = call.data.split(":",1)[1]
    m = InlineKeyboardMarkup()
    names = []
    for p in products.get(cat, []):
        m.add(InlineKeyboardButton(p["name"], callback_data=f"prod:{cat}:{p['name']}"))
        names.append(p["name"])
    m.add(InlineKeyboardButton(L(chat_id, "back"), callback_data="back_main"))
    if names:
        bot.send_message(chat_id, L(chat_id, "items_in_category", cat=cat), reply_markup=m)
    else:
        bot.send_message(chat_id, L(chat_id, "empty_category", cat=cat), reply_markup=m)

@bot.callback_query_handler(func=lambda c: c.data.startswith("prod:"))
def open_product(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    _, cat, name = call.data.split(":",2)
    prod = next((p for p in products.get(cat, []) if p["name"]==name), None)
    if not prod:
        bot.send_message(chat_id, L(chat_id, "product_not_found"))
        return
    caption = f"{prod['name']}\n{prod['desc']}\n{fmt_price(prod['price'])}"
    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton(L(chat_id, "add_to_cart"), callback_data=f"add:{cat}:{name}"))
    m.add(InlineKeyboardButton(L(chat_id, "back"), callback_data=f"cat:{cat}"))
    if prod.get("photo"):
        bot.send_photo(chat_id, prod["photo"], caption=caption, reply_markup=m)
    else:
        bot.send_message(chat_id, caption, reply_markup=m)

@bot.callback_query_handler(func=lambda c: c.data.startswith("add:"))
def add_to_cart(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    _, cat, name = call.data.split(":",2)
    key = f"{cat}:{name}"
    cart = carts.setdefault(chat_id, {})
    if key not in cart:
        cart[key] = {"qty": 0, "comment": ""}
    cart[key]["qty"] += 1
    # Ask optional comment once (if empty)
    if cart[key]["comment"] == "":
        user_step[chat_id] = f"comment:{key}"
        bot.send_message(chat_id, L(chat_id, "comment_prompt"))
    else:
        send_cart(chat_id)

# @bot.message_handler(func=lambda m: isinstance(user_step.get(m.chat.id,''), str) and user_step.get(m.chat.id,'').startswith("comment:"))
# def save_comment(message):
#     chat_id = message.chat.id
#     key = user_step[chat_id].split(":",1)[1]
#     if message.text.strip() != "—":
#         carts.setdefault(chat_id, {}).setdefault(key, {"qty":1,"comment":""})
#         carts[chat_id][key]["comment"] = message.text.strip()
#         bot.send_message(chat_id, L(chat_id, "comment_saved"))
#     user_step[chat_id] = "choose_category"
#     send_cart(chat_id)

# ================= CART =================
# @bot.callback_query_handler(func=lambda c: c.data.startswith("cart:") or c.data.startswith("cart+") or c.data.startswith("cart-"))
# def cart_router(call):
#     chat_id = call.message.chat.id
#     bot.answer_callback_query(call.id)
#     data = call.data
#
#     if data == "cart:open":
#         send_cart(chat_id, call.message.message_id); return
#     if data == "cart:clear":
#         carts[chat_id] = {}; send_cart(chat_id, call.message.message_id); return
#     if data == "cart:order":
#         # for delivery ensure address; pickup allowed without address
#         # (soddalashtirilgan tekshiruv)
#         create_order(chat_id, call.message.message_id); return
#     if data == "cart:usepoints":
#         apply_points(chat_id); send_cart(chat_id, call.message.message_id); return
#
#     # +/-
#     if data.startswith("cart+") or data.startswith("cart-"):
#         sign, pid = data.split(":",1)
#         cart = carts.setdefault(chat_id, {})
#         if pid not in cart: cart[pid] = {"qty":0, "comment":""}
#         cur = cart[pid]["qty"]
#         if sign == "cart+":
#             cart[pid]["qty"] = cur + 1 if cur>0 else 1
#         else:
#             if cur <= 1:
#                 cart.pop(pid,None)
#             else:
#                 cart[pid]["qty"] = cur-1
#         send_cart(chat_id, call.message.message_id); return

# def send_cart(chat_id, msg_id=None):
#     cart = carts.get(chat_id, {})
#     if not cart:
#         m = InlineKeyboardMarkup()
#         m.add(InlineKeyboardButton(L(chat_id, "back"), callback_data="back_main"))
#         if msg_id:
#             try: bot.edit_message_text(L(chat_id, "cart_empty"), chat_id, msg_id, reply_markup=m)
#             except: bot.send_message(chat_id, L(chat_id, "cart_empty"), reply_markup=m)
#         else:
#             bot.send_message(chat_id, L(chat_id, "cart_empty"), reply_markup=m)
#         return
#
#     compute subtotal & apply rules
#     lines, subtotal = [], 0
#     pizza_count = 0
#     for pid, info in cart.items():
#         cat, name = pid.split(":",1)
#         prod = next((p for p in products.get(cat, []) if p["name"]==name), None)
#         if not prod: continue
#         qty = info["qty"]; comment = info.get("comment","")
#         item_total = prod["price"] * qty
#         subtotal += item_total
#         if "pitsa" in cat.lower() or "pitsalar" in cat.lower() or "pitsa" in name.lower() or "pizza" in name.lower():
#             pizza_count += qty
#         cline = f"🍴 {name}\n  {qty} × {fmt_price(prod['price'])} = {fmt_price(item_total)}"
#         if comment: cline += f"\n  " + L(chat_id, "comment_label", cmt=comment)
#         lines.append(cline)
#
#     # Discounts
#     discount_texts = []
#     discount_amount = 0
#     if subtotal >= 100000:
#         disc = int(subtotal * 0.10)
#         discount_amount += disc
#         discount_texts.append("10% (100k+)")
#
#     # Promo code application
#     pcode = user_promos.get(chat_id)
#     if pcode:
#         cursor.execute("SELECT type, value, extra FROM promos WHERE code=? AND active=1", (pcode,))
#         row = cursor.fetchone()
#         if row:
#             ptype, val, extra = row
#             if ptype == "percent":
#                 d = int(subtotal * (val/100.0))
#                 discount_amount += d
#                 discount_texts.append(f"Promo {val}%")
#             elif ptype == "fixed":
#                 discount_amount += val
#                 discount_texts.append(f"Promo {fmt_price(val)}")
#             elif ptype == "bonus_item" and extra:
#                 try:
#                     catx, namex = extra.split(":",1)
#                     lines.append(L(chat_id, "bonus_added", name=namex))
#                 except:
#                     pass
#
#     # Pizza bonus: if 2+ pizzas -> free Cola 0.5L (note only)
#     if pizza_count >= 2:
#         lines.append(L(chat_id, "bonus_added", name="Cola 0.5L"))
#
#     # Delivery fee by distance unless subtotal is above free threshold
#     delivery_fee = BASE_DELIVERY_FEE
#     distance_km = None
#     if subtotal - discount_amount >= FREE_DELIVERY_THRESHOLD:
#         delivery_fee = 0
#     elif chat_id in user_location:
#         lat, lon = user_location[chat_id]
#         distance_km = round(haversine(STORE_LAT, STORE_LON, lat, lon), 1)
#         extra_km = max(0, distance_km - 3.0)
#         delivery_fee = BASE_DELIVERY_FEE + int(extra_km * FEE_PER_KM)
#
#     # Cashback
#     available_pts = get_points(chat_id)
#
#     # Total
#     discounted = max(0, subtotal - discount_amount)
#     total = discounted + delivery_fee
#
#     # Compose text
#     text = L(chat_id, "cart_title") + "\n\n" + "\n".join(lines) + "\n"
#     if discount_texts:
#         text += "\n" + L(chat_id, "discount_applied", txt=", ".join(discount_texts))
#     if distance_km is not None:
#         text += f"\n" + L(chat_id, "distance_fee", km=distance_km, fee=fmt_price(delivery_fee))
#     else:
#         text += f"\n" + L(chat_id, "delivery_fee", fee=fmt_price(delivery_fee))
#     text += f"\n\n" + L(chat_id, "total", total=fmt_price(total))
#     text += f"\n" + L(chat_id, "cashback_info", points=available_pts)
#
#     # Build buttons
#     m = InlineKeyboardMarkup()
#     # rows with - name +
#     for pid, info in cart.items():
#         name = pid.split(":",1)[1]
#         m.row(
#             InlineKeyboardButton("➖", callback_data=f"cart-:{pid}"),
#             InlineKeyboardButton(name, callback_data="noop"),
#             InlineKeyboardButton("➕", callback_data=f"cart+:{pid}")
#         )
#     # action row
#     m.row(InlineKeyboardButton(L(chat_id, "order_btn"), callback_data="cart:order"),
#           InlineKeyboardButton(L(chat_id, "clear_btn"), callback_data="cart:clear"))
#     # points usage
#     if available_pts > 0:
#         m.add(InlineKeyboardButton(L(chat_id, "use_cashback"), callback_data="cart:usepoints"))
#     # promo code
#     m.add(InlineKeyboardButton(L(chat_id, "apply_promo"), callback_data="promo:ask"))
#
#     if msg_id:
#         try: bot.edit_message_text(text, chat_id, msg_id, reply_markup=m)
#         except: bot.send_message(chat_id, text, reply_markup=m)
#     else:
#         bot.send_message(chat_id, text, reply_markup=m)

def get_points(chat_id):
    cursor.execute("SELECT points FROM users WHERE user_id=?", (chat_id,))
    row = cursor.fetchone()
    return int(row[0]) if row else 0

def add_points(chat_id, pts):
    cursor.execute("UPDATE users SET points = COALESCE(points,0) + ? WHERE user_id=?", (int(pts), chat_id))
    conn.commit()

def use_points(chat_id, pts):
    cursor.execute("UPDATE users SET points = MAX(COALESCE(points,0) - ?, 0) WHERE user_id=?", (int(pts), chat_id))
    conn.commit()

def apply_points(chat_id):
    # apply up to 20% of cart discounted subtotal in points
    cart = carts.get(chat_id, {})
    subtotal = 0
    for pid, info in cart.items():
        cat, name = pid.split(":",1)
        prod = next((p for p in products.get(cat, []) if p["name"]==name), None)
        if prod: subtotal += prod["price"] * info["qty"]
    available = get_points(chat_id)
    max_pts = int(0.2 * subtotal)  # up to 20%
    pts_to_use = min(available, max_pts)
    if pts_to_use > 0:
        user_promos[chat_id] = None  # clear promo to avoid double stacking confusion
        cursor.execute("INSERT OR IGNORE INTO promos (code, type, value, active) VALUES ('POINTS_USE','fixed', ?,1)", (pts_to_use,))
        conn.commit()
        user_promos[chat_id] = "POINTS_USE"
        use_points(chat_id, pts_to_use)

# ================= PROMO =================
# === PROMO ASK ===
@bot.callback_query_handler(func=lambda c: c.data == "promo:ask")
def ask_promo(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    user_step[chat_id] = "promo_wait"
    bot.send_message(chat_id, L(chat_id, "promo_ask"))


# === PROMO CODE ENTER ===
@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "promo_wait")
def apply_promo_code(message):
    chat_id = message.chat.id
    code = message.text.strip().upper()

    # Avval promos jadvalini tekshiramiz
    cursor.execute("SELECT type, value, extra, min_amount FROM promos WHERE code=? AND active=1", (code,))
    row = cursor.fetchone()

    if not row:
        bot.send_message(chat_id, "❌ Noto‘g‘ri yoki muddati o‘tgan promokod.")
        user_step[chat_id] = "choose_category"
        send_cart(chat_id)
        return

    # Foydalanuvchi ilgari ishlatganmi?
    cursor.execute("SELECT 1 FROM used_promos WHERE user_id=? AND code=?", (chat_id, code))
    if cursor.fetchone():
        bot.send_message(chat_id, "❌ Siz bu promokoddan foydalanib bo‘lgansiz.")
        user_step[chat_id] = "choose_category"
        send_cart(chat_id)
        return

    # ✅ Promokodni saqlaymiz va DBga yozamiz
    user_promos[chat_id] = code
    cursor.execute("INSERT INTO used_promos (user_id, code) VALUES (?,?)", (chat_id, code))
    conn.commit()

    # ❗ Qo‘shimcha xabar yubormaymiz, faqat savatni yangilaymiz
    user_step[chat_id] = "choose_category"
    send_cart(chat_id)



# === PROMO APPLY FUNCTION ===
def apply_promo(chat_id, subtotal, promo_code):
    cursor.execute("SELECT type, value, extra, min_amount FROM promos WHERE code=? AND active=1", (promo_code,))
    row = cursor.fetchone()
    if not row:
        return subtotal, "❌ Bunday promokod mavjud emas."

    ptype, val, extra, min_amount = row
    if subtotal < min_amount:
        return subtotal, f"❌ Bu promokod faqat {fmt_price(min_amount)} so‘mdan katta buyurtmalarga ishlaydi."

    discount = 0
    msg = ""
    if ptype == "percent":
        discount = int(subtotal * (val / 100.0))
        msg = f"✅ Promokod qo‘llandi! -{val}% chegirma ({fmt_price(discount)})"
    elif ptype == "fixed":
        discount = val
        msg = f"✅ Promokod qo‘llandi! -{fmt_price(val)} chegirma"
    elif ptype == "bonus_item" and extra:
        msg = f"✅ Promokod qo‘llandi! Bonus mahsulot: {extra}"

    new_total = max(0, subtotal - discount)

    # ❗ Muhim: faqat shu yerda ishlatilganini DBga yozamiz
    cursor.execute("INSERT INTO used_promos (user_id, code) VALUES (?, ?)", (chat_id, promo_code))
    conn.commit()

    return new_total, msg





# ================= ORDER CREATION & STATUS =================
def create_order(chat_id, msg_id=None):
    cart = carts.get(chat_id, {})
    if not cart:
        send_cart(chat_id, msg_id); return

    # compute totals similarly to send_cart
    subtotal, pizza_count = 0, 0
    item_rows = []
    for pid, info in cart.items():
        cat, name = pid.split(":",1)
        prod = next((p for p in products.get(cat, []) if p["name"]==name), None)
        if not prod: continue
        qty = info["qty"]; comment = info.get("comment","")
        subtotal += prod["price"] * qty
        if "pitsa" in cat.lower() or "pitsalar" in cat.lower() or "pizza" in name.lower():
            pizza_count += qty
        item_rows.append((cat, name, qty, prod["price"], comment))

    discount_texts = []
    discount_amount = 0
    if subtotal >= 100000:
        d = int(subtotal * 0.10)
        discount_amount += d
        discount_texts.append("10% (100k+)")

    # promo
    pcode = user_promos.get(chat_id)
    if pcode:
        cursor.execute("SELECT type, value, extra FROM promos WHERE code=? AND active=1", (pcode,))
        row = cursor.fetchone()
        if row:
            ptype, val, extra = row
            if ptype == "percent":
                d = int(subtotal * (val/100.0)); discount_amount += d; discount_texts.append(f"Promo {val}%")
            elif ptype == "fixed":
                discount_amount += val; discount_texts.append(f"Promo {val}")
            elif ptype == "bonus_item" and extra:
                discount_texts.append(f"Bonus: {extra}")

    # delivery
    delivery_fee = BASE_DELIVERY_FEE
    if subtotal - discount_amount >= FREE_DELIVERY_THRESHOLD:
        delivery_fee = 0
    elif chat_id in user_location:
        lat, lon = user_location[chat_id]
        dist = haversine(STORE_LAT, STORE_LON, lat, lon)
        delivery_fee = BASE_DELIVERY_FEE + int(max(0, dist - 3.0) * FEE_PER_KM)

    total = max(0, subtotal - discount_amount) + delivery_fee

    # earn cashback
    earn_pts = int((subtotal - discount_amount) * CASHBACK_RATE)
    add_points(chat_id, earn_pts)

    # insert order
    discount_text = ", ".join(discount_texts) if discount_texts else ""
    address = user_address.get(chat_id, "—")
    extra = user_extra.get(chat_id, "—")
    cursor.execute("INSERT INTO orders (user_id, address, extra, status, subtotal, delivery_fee, discount_text, total) VALUES (?,?,?,?,?,?,?,?)",
                   (chat_id, address, extra, "preparing", subtotal, delivery_fee, discount_text, total))
    oid = cursor.lastrowid
    for (cat, name, qty, price, comment) in item_rows:
        cursor.execute("INSERT INTO order_items (order_id, category, name, qty, unit_price, comment) VALUES (?,?,?,?,?,?)",
                       (oid, cat, name, qty, price, comment))
    conn.commit()

    # notify admins
    text = [f"🆕 Order #{oid} (User {chat_id})"]
    for (cat, name, qty, price, comment) in item_rows:
        line = f"{qty} × {name} — {fmt_price(price*qty)}"
        if comment: line += f"\n  [{comment}]"
        text.append(line)
    text.append(f"\nSubtotal: {fmt_price(subtotal)}")
    if discount_text: text.append(f"Discount: {discount_text}")
    text.append(f"Delivery: {fmt_price(delivery_fee)}")
    text.append(f"Total: {fmt_price(total)}")
    text.append(f"Address: {address}")
    text.append(f"Extra: {extra}")
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("⏳", callback_data=f"st:preparing:{oid}"),
          InlineKeyboardButton("🚴", callback_data=f"st:onway:{oid}"),
          InlineKeyboardButton("✅", callback_data=f"st:done:{oid}"))
    for aid in ADMIN_ID:
        try: bot.send_message(aid, "\n".join(text), reply_markup=m)
        except: pass

    # clear cart
    carts[chat_id] = {}
    user_promos[chat_id] = None
    try:
        bot.edit_message_text(L(chat_id, "order_received"), chat_id, msg_id)
    except:
        bot.send_message(chat_id, L(chat_id, "order_received"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("st:"))
def admin_update_status(call):
    if call.message.chat.id not in ADMIN_ID:
        bot.answer_callback_query(call.id); return
    _, status, oid = call.data.split(":",2)
    cursor.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    conn.commit()
    bot.answer_callback_query(call.id, "OK")
    # notify user
    cursor.execute("SELECT user_id FROM orders WHERE id=?", (oid,))
    row = cursor.fetchone()
    if row:
        uid = row[0]
        label = {"preparing":"status_preparing", "onway":"status_onway", "done":"status_done"}.get(status, "status_preparing")
        bot.send_message(uid, L(uid, "status_updated", status=L(uid, label)))

# ================= HISTORY / RE-ORDER / RATING =================
@bot.callback_query_handler(func=lambda c: c.data == "my_orders")
def show_history(call):
    chat_id = call.message.chat.id
    cursor.execute("SELECT id, total, status, created FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 5", (chat_id,))
    rows = cursor.fetchall()
    if not rows:
        bot.send_message(chat_id, L(chat_id, "order_history_title") + "—")
        return
    text = L(chat_id, "order_history_title")
    m = InlineKeyboardMarkup()
    for (oid, total, status, created) in rows:
        text += f"#{oid} — {fmt_price(total)} — {status} — {created}\n"
        m.add(InlineKeyboardButton(L(chat_id, "reorder") + f" #{oid}", callback_data=f"reorder:{oid}"))
    bot.send_message(chat_id, text, reply_markup=m)

@bot.callback_query_handler(func=lambda c: c.data.startswith("reorder:"))
def reorder(call):
    chat_id = call.message.chat.id
    oid = int(call.data.split(":")[1])
    cursor.execute("SELECT category, name, qty FROM order_items WHERE order_id=?", (oid,))
    rows = cursor.fetchall()
    if not rows:
        bot.answer_callback_query(call.id, "No items"); return
    cart = carts.setdefault(chat_id, {})
    for (cat, name, qty) in rows:
        key = f"{cat}:{name}"
        e = cart.get(key, {"qty":0,"comment":""})
        e["qty"] += qty
        cart[key] = e
    bot.answer_callback_query(call.id, "OK")
    bot.send_message(chat_id, L(chat_id, "reorder_ok"))
    send_cart(chat_id)

@bot.message_handler(commands=['rate'])
def manual_rate(message):
    parts = message.text.split()
    if len(parts) < 2: return
    oid = parts[1]
    m = InlineKeyboardMarkup()
    for s in range(1,6):
        m.add(InlineKeyboardButton("⭐"*s, callback_data=f"rate:{oid}:{s}"))
    bot.send_message(message.chat.id, L(message.chat.id, "rate_order"), reply_markup=m)

@bot.callback_query_handler(func=lambda c: c.data.startswith("rate:"))
def save_rating(call):
    _, oid, star = call.data.split(":",2)
    cursor.execute("INSERT INTO ratings (order_id, user_id, stars) VALUES (?,?,?)",
                   (int(oid), call.message.chat.id, int(star)))
    conn.commit()
    bot.answer_callback_query(call.id, "OK")
    try:
        bot.edit_message_text(L(call.message.chat.id, "thanks_rating"), call.message.chat.id, call.message.message_id)
    except:
        bot.send_message(call.message.chat.id, L(call.message.chat.id, "thanks_rating"))

# ================= SETTINGS =================
@bot.callback_query_handler(func=lambda c: c.data == "settings")
def open_settings(call):
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(L(call.message.chat.id, "settings_menu"), call.message.chat.id, call.message.message_id, reply_markup=settings_menu(call.message.chat.id))
    except:
        bot.send_message(call.message.chat.id, L(call.message.chat.id, "settings_menu"), reply_markup=settings_menu(call.message.chat.id))

@bot.callback_query_handler(func=lambda c: c.data == "set_lang")
def choose_lang(call):
    bot.answer_callback_query(call.id)
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("O‘zbekcha", callback_data="lang:uz"),
          InlineKeyboardButton("Русский", callback_data="lang:ru"))
    try:
        bot.edit_message_text(L(call.message.chat.id, "choose_lang"), call.message.chat.id, call.message.message_id, reply_markup=m)
    except:
        bot.send_message(call.message.chat.id, L(call.message.chat.id, "choose_lang"), reply_markup=m)

@bot.callback_query_handler(func=lambda c: c.data.startswith("lang:"))
def switch_lang(call):
    lang = call.data.split(":")[1]
    set_lang(call.message.chat.id, lang)
    bot.answer_callback_query(call.id, L(call.message.chat.id, "lang_switched", lang=lang))
    try:
        bot.edit_message_text(L(call.message.chat.id, "settings_menu"), call.message.chat.id, call.message.message_id, reply_markup=settings_menu(call.message.chat.id))
    except:
        bot.send_message(call.message.chat.id, L(call.message.chat.id, "settings_menu"), reply_markup=settings_menu(call.message.chat.id))

@bot.callback_query_handler(func=lambda c: c.data == "toggle_push")
def toggle_push(call):
    chat_id = call.message.chat.id
    cursor.execute("UPDATE users SET push = 1 - COALESCE(push,1) WHERE user_id=?", (chat_id,))
    conn.commit()
    state = "ON" if get_push(chat_id) else "OFF"
    bot.answer_callback_query(call.id, L(chat_id, "push_switched", state=state))
    try:
        bot.edit_message_text(L(chat_id, "settings_menu"), chat_id, call.message.message_id, reply_markup=settings_menu(chat_id))
    except:
        bot.send_message(chat_id, L(chat_id, "settings_menu"), reply_markup=settings_menu(chat_id))

# ================= ADMIN =================
@bot.callback_query_handler(func=lambda c: c.data == "admin")
def open_admin(call):
    if call.message.chat.id not in ADMIN_ID:
        bot.answer_callback_query(call.id); return
    try:
        bot.edit_message_text("ADMIN", call.message.chat.id, call.message.message_id, reply_markup=admin_menu(call.message.chat.id))
    except:
        bot.send_message(call.message.chat.id, "ADMIN", reply_markup=admin_menu(call.message.chat.id))

@bot.callback_query_handler(func=lambda c: c.data == "adm_stats")
def admin_stats(call):
    if call.message.chat.id not in ADMIN_ID: return
    q = """
    SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE DATE(created)=DATE('now','localtime');
    """
    cursor.execute(q); dcnt, dsum = cursor.fetchone()
    q = "SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE DATE(created)>=DATE('now','-7 day','localtime');"
    cursor.execute(q); wcnt, wsum = cursor.fetchone()
    q = "SELECT COUNT(*), COALESCE(SUM(total),0) FROM orders WHERE strftime('%Y-%m', created)=strftime('%Y-%m','now','localtime');"
    cursor.execute(q); mcnt, msum = cursor.fetchone()
    text = f"Bugun: {dcnt} ta / {fmt_price(int(dsum))}\n7 kun: {wcnt} ta / {fmt_price(int(wsum))}\nOy: {mcnt} ta / {fmt_price(int(msum))}"
    bot.send_message(call.message.chat.id, text)

@bot.callback_query_handler(func=lambda c: c.data == "adm_top")
def admin_top(call):
    if call.message.chat.id not in ADMIN_ID: return
    q = "SELECT user_id, COUNT(*), SUM(total) FROM orders GROUP BY user_id ORDER BY SUM(total) DESC LIMIT 10"
    cursor.execute(q); rows = cursor.fetchall()
    lines = ["TOP-10 mijozlar:"]
    for uid, cnt, sm in rows:
        lines.append(f"{uid}: {cnt} ta / {fmt_price(int(sm))}")
    bot.send_message(call.message.chat.id, "\n".join(lines))

@bot.callback_query_handler(func=lambda c: c.data == "adm_broadcast")
def admin_broadcast(call):
    if call.message.chat.id not in ADMIN_ID: return
    user_step[call.message.chat.id] = "broadcast"
    bot.send_message(call.message.chat.id, L(call.message.chat.id, "enter_broadcast"))

@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "broadcast")
def do_broadcast(message):
    if message.chat.id not in ADMIN_ID: return
    text = message.text
    ok=fail=0
    cursor.execute("SELECT user_id, push FROM users")
    for uid, push in cursor.fetchall():
        if not push: continue
        try:
            bot.send_message(uid, text); ok+=1
        except: fail+=1
    cursor.execute("INSERT INTO broadcasts (text, sent, fails) VALUES (?,?,?)", (text, ok, fail))
    conn.commit()
    user_step[message.chat.id] = None
    bot.send_message(message.chat.id, L(message.chat.id, "broadcast_done", ok=ok, fail=fail))

@bot.callback_query_handler(func=lambda c: c.data == "adm_promo")
def admin_promo(call):
    if call.message.chat.id not in ADMIN_ID: return
    user_step[call.message.chat.id] = "promo_gen"
    bot.send_message(call.message.chat.id, "Format: type|value|extra (mas: percent|10|, fixed|20000|, bonus_item|🍋 Limonadlar:Cola 0.5L| )")

@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "promo_gen")
def promo_gen(message):
    if message.chat.id not in ADMIN_ID: return
    try:
        t, v, e = message.text.split("|",2)
        code = rand_code(8)
        cursor.execute("INSERT OR REPLACE INTO promos (code, type, value, extra, active) VALUES (?,?,?,?,1)", (code, t.strip(), int(v), e.strip()))
        conn.commit()
        user_step[message.chat.id] = None
        bot.send_message(message.chat.id, f"Promo tayyor: {code}")
    except Exception as ex:
        bot.send_message(message.chat.id, f"Xato: {ex}")

@bot.callback_query_handler(func=lambda c: c.data == "adm_daily")
def admin_daily(call):
    if call.message.chat.id not in ADMIN_ID: return
    user_step[call.message.chat.id] = "daily_set"
    bot.send_message(call.message.chat.id, L(call.message.chat.id, "enter_daily_deal"))

@bot.message_handler(func=lambda m: user_step.get(m.chat.id) == "daily_set")
def daily_set(message):
    if message.chat.id not in ADMIN_ID: return
    try:
        p, price = message.text.split("|",1)
        cursor.execute("UPDATE daily_deal SET product=?, price=? WHERE id=1", (p.strip(), int(price)))
        conn.commit()
        user_step[message.chat.id] = None
        bot.send_message(message.chat.id, L(message.chat.id, "daily_set"))
    except Exception as ex:
        bot.send_message(message.chat.id, f"Xato: {ex}")

# ================= VOICE PLACEHOLDER (no STT) =================
@bot.message_handler(content_types=['voice'])
def voice_placeholder(message):
    bot.send_message(message.chat.id, "🎙 Ovozli buyurtma funksiyasi hozircha tayyor emas. Matn orqali buyurtma bering.")


# --- ADMIN: Mahsulot qo‘shish ---
adding_product = {}

@bot.callback_query_handler(func=lambda call: call.data == "admin_add")
def admin_choose_category(call):
    markup = InlineKeyboardMarkup()
    for cat in products.keys():
        markup.add(InlineKeyboardButton(cat, callback_data=f"addcat:{cat}"))
    bot.edit_message_text(
        "🗂 Qaysi kategoriya uchun mahsulot qo‘shmoqchisiz?",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("addcat:"))
def admin_add_photo(call):
    category = call.data.split("addcat:")[1]
    adding_product[call.message.chat.id] = {
        "step": "get_photo",
        "category": category
    }
    bot.send_message(call.message.chat.id,
                     f"📷 <b>{category}</b> kategoriyasi uchun mahsulot rasmini yuboring:",
                     parse_mode="HTML")

@bot.message_handler(content_types=['photo'])
def get_photo(message):
    if message.chat.id in adding_product and adding_product[message.chat.id]["step"] == "get_photo":
        adding_product[message.chat.id]["photo"] = message.photo[-1].file_id
        adding_product[message.chat.id]["step"] = "get_name"
        bot.send_message(message.chat.id, "✏️ Mahsulot nomini yuboring:")

@bot.message_handler(func=lambda m: m.chat.id in adding_product)
def get_product_info(message):
    step = adding_product[message.chat.id]["step"]

    if step == "get_name":
        adding_product[message.chat.id]["name"] = message.text
        adding_product[message.chat.id]["step"] = "get_desc"
        bot.send_message(message.chat.id, "📝 Mahsulot tavsifini yuboring:")

    elif step == "get_desc":
        adding_product[message.chat.id]["desc"] = message.text
        adding_product[message.chat.id]["step"] = "get_price"
        bot.send_message(message.chat.id, "💰 Mahsulot narxini yuboring (faqat son):")

    elif step == "get_price":
        try:
            price = int(message.text)
            adding_product[message.chat.id]["price"] = price

            category = adding_product[message.chat.id]["category"]
            products[category].append({
                "photo": adding_product[message.chat.id]["photo"],
                "name": adding_product[message.chat.id]["name"],
                "desc": adding_product[message.chat.id]["desc"],
                "price": adding_product[message.chat.id]["price"]
            })

            save_products()
            bot.send_message(message.chat.id, "✅ Mahsulot muvaffaqiyatli qo‘shildi!")
            del adding_product[message.chat.id]

        except ValueError:
            bot.send_message(message.chat.id, "❌ Narx faqat son bo‘lishi kerak.")

PROMO_FILE = "promocodes.json"

# --- PROMOKODLAR ---
def load_promos():
    if os.path.exists(PROMO_FILE):
        with open(PROMO_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_promos():
    with open(PROMO_FILE, "w", encoding="utf-8") as f:
        json.dump(promocodes, f, ensure_ascii=False, indent=4)

promocodes = load_promos()

adding_promo = {}

@bot.callback_query_handler(func=lambda call: call.data == "admin_promo")
def create_promo(call):
    adding_promo[call.message.chat.id] = {"step": "get_code"}
    bot.send_message(call.message.chat.id, "🎟 Promokod nomini yuboring (masalan: DISCOUNT2025):")


@bot.message_handler(func=lambda m: m.chat.id in adding_promo)
def get_promo_info(message):
    step = adding_promo[message.chat.id]["step"]

    # 1️⃣ Promokod nomi
    if step == "get_code":
        code = message.text.strip().upper()
        adding_promo[message.chat.id]["code"] = code
        adding_promo[message.chat.id]["step"] = "get_discount"
        bot.send_message(message.chat.id,
                         "💰 Chegirma summasini yuboring (masalan: 20000 yoki 10%):")

    # 2️⃣ Chegirma qiymati (summali yoki %)
    elif step == "get_discount":
        val = message.text.strip()
        if val.endswith("%"):  # foizli chegirma
            try:
                discount = int(val.replace("%", "").strip())
                adding_promo[message.chat.id]["type"] = "percent"
                adding_promo[message.chat.id]["value"] = discount
            except:
                bot.send_message(message.chat.id, "❌ Noto‘g‘ri format. Masalan: 10% yoki 20000")
                return
        else:  # summali chegirma
            try:
                discount = int(val)
                adding_promo[message.chat.id]["type"] = "fixed"
                adding_promo[message.chat.id]["value"] = discount
            except:
                bot.send_message(message.chat.id, "❌ Noto‘g‘ri format. Masalan: 10% yoki 20000")
                return

        adding_promo[message.chat.id]["step"] = "get_min"
        bot.send_message(message.chat.id, "📊 Minimal buyurtma summasini yuboring (masalan: 50000):")

    # 3️⃣ Minimal buyurtma summasi
    elif step == "get_min":
        try:
            min_order = int(message.text)
            code = adding_promo[message.chat.id]["code"]
            ptype = adding_promo[message.chat.id]["type"]
            value = adding_promo[message.chat.id]["value"]

            # Bazaga yozamiz
            cursor.execute(
                "INSERT INTO promos (code, type, value, min_amount, active) VALUES (?, ?, ?, ?, 1)",
                (code, ptype, value, min_order)
            )
            conn.commit()

            bot.send_message(message.chat.id,
                             f"✅ Promokod yaratildi!\n\n"
                             f"🔑 Kod: {code}\n"
                             f"💰 Chegirma: {value}{'%' if ptype=='percent' else ' so‘m'}\n"
                             f"📊 Minimal buyurtma: {min_order} so‘m")

            del adding_promo[message.chat.id]
        except ValueError:
            bot.send_message(message.chat.id, "❌ Minimal summa faqat son bo‘lishi kerak.")

# def apply_promo(chat_id, total, promo_code):
#     promo = promocodes.get(promo_code)
#     if not promo:
#         return total, "❌ Bunday promokod mavjud emas."
#
#     if total < promo["min_order"]:
#         return total, f"❌ Bu promokod faqat {promo['min_order']} so‘mdan katta buyurtmalarga ishlaydi."
#
#     total -= promo["discount"]
#     if total < 0:
#         total = 0
#     return total, f"✅ Promokod qo‘llandi! -{promo['discount']} so‘m chegirma"
#



# ================= BRANCHES =================
# 1) CONFIG bo'limidan keyin joylashtiring
BRANCHES = [
    {
        "id": "yangiyol2",
        "name": "🏠 Yangiyo'l 2",
        "address": "📍 Ташкентская область, Янгиюльский район, населённый пункт Ниязбаш, улица O. Кучкарова, 2А",
        # 👇 Link koddagi koordinatalardan generatsiya qilinadi
        "lat": 41.109473,
        "lon": 69.064478,
        "hours": "🕑 10:00-04:45"
    },
    # Yana filial qo‘shmoqchi bo‘lsangiz:
    # {"id":"...", "name":"🏠 ...", "address":"📍 ...", "lat": ..., "lon": ..., "hours":"🕑 ..."}
]

def branches_menu(chat_id):
    """Filiallar ro‘yxati uchun inline menu"""
    m = InlineKeyboardMarkup()
    # Har bir filial uchun alohida tugma
    for br in BRANCHES:
        m.add(InlineKeyboardButton(br["name"], callback_data=f"branch:{br['id']}"))
    # Orqaga
    m.add(InlineKeyboardButton(L(chat_id, "back"), callback_data="back_main"))
    return m

def branch_by_id(bid):
    for br in BRANCHES:
        if br["id"] == bid:
            return br
    return None

@bot.callback_query_handler(func=lambda c: c.data == "branches")
def show_branches(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    count = len(BRANCHES)

    header = L(chat_id, "branches_counts").format(count=count)

    try:
        bot.edit_message_text(
            header,
            chat_id,
            call.message.message_id,
            reply_markup=branches_menu(chat_id),
            parse_mode="HTML"
        )
    except:
        bot.send_message(
            chat_id,
            header,
            reply_markup=branches_menu(chat_id),
            parse_mode="HTML"
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("branch:"))
def open_branch(call):
    bot.answer_callback_query(call.id)
    chat_id = call.message.chat.id
    bid = call.data.split(":", 1)[1]
    br = branch_by_id(bid)
    if not br:
        bot.send_message(chat_id, "❌ Filial topilmadi.")
        return

    map_link = f"http://maps.yandex.ru/?text={br['lat']},{br['lon']}"
    text = (
        f"{br['name']}\n\n"
        f"{br['address']} <a href='{map_link}'>{L(chat_id, 'branch_open_map')}</a>\n\n"
        f"{br['hours']}\n\n"
        f"{L(chat_id, 'branches_count').format(count=len(BRANCHES))}"
    )

    m = InlineKeyboardMarkup()
    m.add(InlineKeyboardButton("⬅️ " + L(chat_id, "branches"), callback_data="branches"))
    m.add(InlineKeyboardButton(L(chat_id, "back"), callback_data="back_main"))

    bot.send_message(chat_id, text, reply_markup=m, parse_mode="HTML")



# ================= RUN =================
if __name__ == "__main__":
    print("UJO Food Full bot started.")
    bot.infinity_polling()
