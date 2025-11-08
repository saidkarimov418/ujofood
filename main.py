import telebot
from config import TOKEN
from keyboards import main_menu_markup
from i18n import L

bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, L(chat_id, "main_menu"), reply_markup=main_menu_markup(chat_id))

if __name__ == "__main__":
    print("✅ Bot ishga tushdi")
    bot.polling(none_stop=True)
