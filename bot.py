import os
import re
import json
import uuid
import psycopg2
from docx import Document
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    CallbackQueryHandler,
    PollAnswerHandler
)

TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = "quizerrsbot"
DATABASE_URL = os.getenv("DATABASE_URL")

# Temporary memory per user (before saving quiz)
temp_uploads = {}

# Active quiz sessions
user_sessions = {}
# user_id -> {"quiz_id": str, "index": int, "score": int, "chat_id": int, "questions": list}


# -------------------- DATABASE --------------------

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS quizzes (
        quiz_id TEXT PRIMARY KEY,
        quiz_name TEXT NOT NULL,
        questions_json TEXT NOT NULL
    )
    """)

    conn.commit()
    cur.close()
    conn.close()


def save_quiz_to_db(quiz_id, quiz_name, questions):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO quizzes (quiz_id, quiz_name, questions_json) VALUES (%s, %s, %s)",
        (quiz_id, quiz_name, json.dumps(questions, ensure_ascii=False))
    )

    conn.commit()
    cur.close()
    conn.close()


def load_quiz_from_db(quiz_id):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT quiz_name, questions_json FROM quizzes WHERE quiz_id = %s", (quiz_id,))
    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        return None

    quiz_name, questions_json = row
    questions = json.loads(questions_json)

    return {"quiz_name": quiz_name, "questions": questions}


# -------------------- DOCX PARSER --------------------

def parse_docx(file_path):
    doc = Document(file_path)

    lines = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if text:
            lines.append(text)

    full_text = "\n".join(lines)

    # split by ---
    blocks = re.split(r"\n\s*---\s*\n", full_text)

    questions = []

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        q_match = re.search(r"Q:\s*(.+)", block)
        if not q_match:
            continue

        question_text = q_match.group(1).strip()

        tf_match = re.search(r"TYPE:\s*TF", block, re.IGNORECASE)

        explanation_match = re.search(r"EXPLANATION:\s*(.+)", block, re.DOTALL)
        explanation = explanation_match.group(1).strip() if explanation_match else "No explanation provided."

        answer_match = re.search(r"ANSWER:\s*(.+)", block)
        if not answer_match:
            continue
        answer_raw = answer_match.group(1).strip()

        if tf_match:
            options = ["True", "False"]
            correct = 0 if answer_raw.lower() == "true" else 1

        else:
            options = []
            for letter in ["A", "B", "C", "D"]:
                opt_match = re.search(rf"{letter}\)\s*(.+)", block)
                if opt_match:
                    options.append(opt_match.group(1).strip())

            if len(options) < 2:
                continue

            correct_letter = answer_raw.upper().strip()
            correct_map = {"A": 0, "B": 1, "C": 2, "D": 3}

            if correct_letter not in correct_map:
                continue

            correct = correct_map[correct_letter]

        questions.append({
            "question": question_text,
            "options": options,
            "correct": correct,
            "explanation": explanation
        })

    return questions


# -------------------- BOT HANDLERS --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    # If started with a quiz link
    if args:
        quiz_id = args[0].strip()

        quiz_data = load_quiz_from_db(quiz_id)
        if not quiz_data:
            await update.message.reply_text("❌ Quiz not found. The link may be invalid.")
            return

        quiz_name = quiz_data["quiz_name"]

        keyboard = [
            [InlineKeyboardButton("▶️ Start your quiz", callback_data=f"STARTQUIZ|{quiz_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"📌 Quiz Name: {quiz_name}\n\nPress the button below to start 👇",
            reply_markup=reply_markup
        )
        return

    await update.message.reply_text(
        "👋 Welcome!\n\n"
        "📌 Send me a DOCX file containing your quiz questions.\n"
        "Then I will ask you for the Quiz Name.\n\n"
        "After saving, I will generate a share link 🔗"
    )


async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    doc_file = update.message.document

    if not doc_file.file_name.endswith(".docx"):
        await update.message.reply_text("❌ Please send a .docx file only.")
        return

    file = await doc_file.get_file()
    os.makedirs("downloads", exist_ok=True)

    path = f"downloads/{user_id}_quiz.docx"
    await file.download_to_drive(path)

    questions = parse_docx(path)

    if not questions:
        await update.message.reply_text("❌ No valid questions found. Please check formatting.")
        return

    temp_uploads[user_id] = {
        "questions": questions,
        "chat_id": chat_id
    }

    await update.message.reply_text(
        f"✅ File received!\n📌 Questions extracted: {len(questions)}\n\n"
        "📝 Now send me the Quiz Name (example: Chapter 3 - Blood)."
    )


async def handle_quiz_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in temp_uploads:
        return

    quiz_name = update.message.text.strip()
    questions = temp_uploads[user_id]["questions"]

    quiz_id = "q_" + uuid.uuid4().hex[:8]

    save_quiz_to_db(quiz_id, quiz_name, questions)

    del temp_uploads[user_id]

    link = f"https://t.me/{BOT_USERNAME}?start={quiz_id}"

    await update.message.reply_text(
        f"🎉 Quiz saved successfully!\n\n"
        f"📌 Quiz Name: {quiz_name}\n"
        f"🧾 Questions: {len(questions)}\n\n"
        f"🔗 Share Link:\n{link}"
    )


async def start_quiz_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    chat_id = query.message.chat_id

    _, quiz_id = query.data.split("|")

    quiz_data = load_quiz_from_db(quiz_id)
    if not quiz_data:
        await query.message.reply_text("❌ Quiz not found.")
        return

    questions = quiz_data["questions"]

    user_sessions[user_id] = {
        "quiz_id": quiz_id,
        "index": 0,
        "score": 0,
        "chat_id": chat_id,
        "questions": questions
    }

    await query.message.reply_text("✅ Starting your quiz now...")

    await send_next_question(user_id, context)


async def send_next_question(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = user_sessions.get(user_id)
    if not session:
        return

    idx = session["index"]
    questions = session["questions"]
    chat_id = session["chat_id"]

    if idx >= len(questions):
        score = session["score"]
        total = len(questions)
        percent = round((score / total) * 100, 1)

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎉 Quiz Finished!\n\n🏆 Score: {score}/{total}\n📊 Percentage: {percent}%"
        )

        del user_sessions[user_id]
        return

    q = questions[idx]

    await context.bot.send_poll(
        chat_id=chat_id,
        question=f"Q{idx+1}: {q['question']}",
        options=q["options"],
        type="quiz",
        correct_option_id=q["correct"],
        explanation=f"💡 {q['explanation']}",
        is_anonymous=False
    )

    session["index"] += 1


async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.poll_answer.user.id

    session = user_sessions.get(user_id)
    if not session:
        return

    question_index = session["index"] - 1
    if question_index < 0:
        return

    q = session["questions"][question_index]

    if update.poll_answer.option_ids:
        chosen = update.poll_answer.option_ids[0]
        if chosen == q["correct"]:
            session["score"] += 1

    await send_next_question(user_id, context)


# -------------------- MAIN --------------------

def main():
    if not TOKEN:
        print("❌ BOT_TOKEN is missing!")
        return

    if not DATABASE_URL:
        print("❌ DATABASE_URL is missing!")
        return

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))

    # Quiz name handler (only after upload)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_quiz_name))

    # Start quiz button handler
    app.add_handler(CallbackQueryHandler(start_quiz_button, pattern=r"^STARTQUIZ\|"))

    # Correct handler for poll answers
    app.add_handler(PollAnswerHandler(poll_answer))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
