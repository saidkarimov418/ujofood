from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import ADMIN_ID
from i18n import L

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
