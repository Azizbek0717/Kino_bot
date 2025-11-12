import telebot
import time

# 🔐 Bot token va kanal ID (sening ma'lumotlaring bilan)
TOKEN = "8537929828:AAG-_7f__DV7NK_-tuBkfZybwTfj5uB7qV4"
CHANNEL_ID = -1003327565529  # @kino_bot_bazasi kanal ID

bot = telebot.TeleBot(TOKEN)

# /start buyrug'i
@bot.message_handler(commands=['start'])
def start_message(message):
    bot.reply_to(message, "🎬 Assalomu alaykum!\nKino kodini kiriting. Masalan: 147")

# Kodni tekshirish va kino yuborish
@bot.message_handler(func=lambda message: True)
def send_movie(message):
    if message.text == "147":
        bot.send_video(
            message.chat.id,
            "https://t.me/kino_bot_bazasi/1",
            caption="🎥 *YIRTQICH*\nTili: o'zbek",
            parse_mode="Markdown"
        )
    else:
        bot.reply_to(message, "❌ Kod topilmadi")

# 🔄 Botni doimiy ishlatish
while True:
    try:
        bot.polling(none_stop=True)
    except Exception as e:
        print("Xatolik:", e)
        time.sleep(5)
