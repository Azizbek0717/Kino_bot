import sqlite3
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler, MessageHandler, filters
)

# ---------- TOKEN VA ADMIN ----------
TOKEN = os.getenv("8537929828:AAG-_7f__DV7NK_-tuBkfZybwTfj5uB7qV4")  # Render Environment Variable
ADMIN_ID = 6022023269

# ---------- DATABASE ----------
db = sqlite3.connect("kino.db", check_same_thread=False)
sql = db.cursor()

sql.execute("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY
)
""")

sql.execute("""
CREATE TABLE IF NOT EXISTS movies(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    description TEXT
)
""")
db.commit()

# ---------- START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sql.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    db.commit()

    await update.message.reply_text(
        "🎬 Kino kodini yuboring 🔍\n\n"
        "📌 /movies - Kinolar ro'yhati\n"
        "📌 /admin - Admin panel"
    )

# ---------- ADMIN PANEL ----------
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Siz admin emassiz!")
        return

    keyboard = [
        [InlineKeyboardButton("📥 Kino qo'shish", callback_data="add_movie")],
        [InlineKeyboardButton("📊 Statistika", callback_data="stats")],
        [InlineKeyboardButton("📢 Xabar yuborish", callback_data="send_message")]
    ]
    await update.message.reply_text(
        "👑 Admin panel:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ---------- SHOW MOVIES ----------
async def show_movies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    movies = sql.execute("SELECT id, title FROM movies").fetchall()
    if not movies:
        await update.message.reply_text("Hozircha kino yo'q!")
        return

    keyboard = [
        [InlineKeyboardButton(m[1], callback_data=f"movie_{m[0]}")]
        for m in movies
    ]
    keyboard.append([InlineKeyboardButton("📊 Statistika", callback_data="stats")])

    await update.message.reply_text(
        "🎬 Kinolar ro'yhati:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ---------- CALLBACK ----------
user_steps = {}

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data == "stats":
        users = sql.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        movies = sql.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
        await query.edit_message_text(
            f"👤 Foydalanuvchilar: {users}\n🎬 Kinolar: {movies}"
        )
        return

    if user_id == ADMIN_ID:
        if data == "add_movie":
            user_steps[user_id] = "awaiting_title"
            await query.edit_message_text("📌 Kino nomini kiriting:")
            return

        if data == "send_message":
            user_steps[user_id] = "awaiting_message"
            await query.edit_message_text("📢 Xabar matnini kiriting:")
            return

    if data.startswith("movie_"):
        movie_id = int(data.split("_")[1])
        movie = sql.execute(
            "SELECT title, description FROM movies WHERE id=?",
            (movie_id,)
        ).fetchone()
        if movie:
            await query.edit_message_text(f"🎬 {movie[0]}\n\n📄 {movie[1]}")

# ---------- ADMIN TEXT ----------
async def admin_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if user_id != ADMIN_ID:
        return

    step = user_steps.get(user_id)

    if step == "awaiting_title":
        user_steps[user_id] = {"title": text}
        await update.message.reply_text("📌 Tavsifni kiriting:")
        return

    if isinstance(step, dict):
        title = step["title"]
        sql.execute(
            "INSERT INTO movies(title, description) VALUES (?, ?)",
            (title, text)
        )
        db.commit()
        user_steps.pop(user_id)
        await update.message.reply_text("✅ Kino qo'shildi!")
        return

    if step == "awaiting_message":
        users = sql.execute("SELECT user_id FROM users").fetchall()
        for u in users:
            try:
                await context.bot.send_message(u[0], text)
            except:
                pass
        user_steps.pop(user_id)
        await update.message.reply_text("✅ Xabar yuborildi!")

# ---------- MAIN ----------
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("movies", show_movies))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_messages))

    print("Bot ishga tushdi...")
    app.run_polling()

if __name__ == "__main__":
    main()