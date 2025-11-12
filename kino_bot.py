import telebot

# Tokenni yangisiga almashtirishni tavsiya qilaman!
TOKEN = "8537929828:AAG-_7f__DV7NK_-tuBkfZybwTfj5uB7qV4"
bot = telebot.TeleBot(TOKEN)

# ✅ Kino bazasi — to'g'ri file_id bilan
movies = {
    "147": {
        "title": "YIRTQICH",
        "video": "BAACAgIAAxkBAAMJaRMrhAFYpbSIo2TAhhq_v8oLol8AAt1hAAK3zClLS5vFryiYxRw2BA",
        "info": "Tili: o'zbek"
    }
}

# ✅ /start komandasi
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Assalomu alaykum!\nKino kodini kiriting. Masalan: 147")

# ✅ Kino yuborish
@bot.message_handler(func=lambda msg: msg.text.isdigit())
def send_movie(message):
    code = message.text.strip()

    if code in movies:
        movie = movies[code]
        caption = f"{movie['title']}\n\n{movie['info']}"
        bot.send_video(message.chat.id, movie['video'], caption=caption)
    else:
        bot.reply_to(message, "Kod topilmadi ❌")

# ✅ KANALDAN file_id olish (botni kanalga admin qiling)
@bot.channel_post_handler(content_types=['video'])
def get_channel_video(message):
    file_id = message.video.file_id
    bot.send_message(6022023269, f"FILE ID:\n{file_id}")

# ✅ Botni ishga tushurish
bot.polling()
