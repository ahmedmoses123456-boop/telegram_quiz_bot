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

temp_uploads = {}
user_sessions = {}
# user_id -> {
#   "quiz_id": str,
#   "index": int,
#   "score": int,
#   "chat_id": int,
#   "questions": list,
#   "waiting_for_timer": bool,
#   "time_per_question": int
# }


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


# -------------------- DOCX PARSERS --------------------

def parse_docx_old_format(full_text: str):
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


def parse_docx_table(doc: Document):
    """
    Reads questions from DOCX tables.

    Required columns:
    Type | Question | A | B | C | D | Correct | Explanation

    Type values:
    MCQ or TF
    """

    questions = []

    for table in doc.tables:
        rows = table.rows
        if len(rows) < 2:
            continue

        # Read headers
        headers = [cell.text.strip().lower() for cell in rows[0].cells]

        required = ["type", "question", "correct", "explanation"]
        if not all(r in headers for r in required):
            continue

        def get_cell(row, col_name):
            if col_name not in headers:
                return ""
            idx = headers.index(col_name)
            if idx >= len(row.cells):
                return ""
            return row.cells[idx].text.strip()

        for r in rows[1:]:
            q_type = get_cell(r, "type").strip().upper()
            q_text = get_cell(r, "question").strip()
            correct_val = get_cell(r, "correct").strip()
            explanation = get_cell(r, "explanation").strip()

            if not q_text:
                continue

            if not explanation:
                explanation = "No explanation provided."

            if q_type == "TF":
                options = ["True", "False"]

                if correct_val.lower() == "true":
                    correct = 0
                elif correct_val.lower() == "false":
                    correct = 1
                else:
                    continue

                questions.append({
                    "question": q_text,
                    "options": options,
                    "correct": correct,
                    "explanation": explanation
                })

            else:
                # Default MCQ
                a = get_cell(r, "a")
                b = get_cell(r, "b")
                c = get_cell(r, "c")
                d = get_cell(r, "d")

                options = [a, b, c, d]
                options = [opt.strip() for opt in options if opt.strip()]

                if len(options) < 2:
                    continue

                correct_letter = correct_val.upper().strip()
                correct_map = {"A": 0, "B": 1, "C": 2, "D": 3}

                if correct_letter not in correct_map:
                    continue

                correct = correct_map[correct_letter]

                if correct >= len(options):
                    continue

                questions.append({
                    "question": q_text,
                    "options": options,
                    "correct": correct,
                    "explanation": explanation
                })

    return questions


def parse_docx(file_path):
    doc = Document(file_path)

    # 1) Try reading tables first (new method)
    table_questions = parse_docx_table(doc)
    if table_questions:
        return table_questions

    # 2) If no valid tables found, fallback to old text format
    lines = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if text:
            lines.append(text)

    full_text = "\n".join(lines)

    old_questions = parse_docx_old_format(full_text)
    return old_questions


# -------------------- BOT HANDLERS --------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    # Start from quiz link
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

    # Normal start message
    await update.message.reply_text(
        "Welcome!\n"
        "Send me a DOCX file containing your quiz questions."
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
        await update.message.reply_text(
            "❌ No valid questions found.\n\n"
            "📌 Supported formats:\n"
            "1) Old format (Q:, A), ANSWER:, EXPLANATION:, ---)\n"
            "2) DOCX Table format (Type, Question, A, B, C, D, Correct, Explanation)"
        )
        return

    temp_uploads[user_id] = {
        "questions": questions,
        "chat_id": chat_id
    }

    await update.message.reply_text(
        f"✅ File received!\nQuestions extracted: {len(questions)}\n\n"
        "📝 Now send me the Quiz Name."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # ---------------- Timer Step ----------------
    session = user_sessions.get(user_id)
    if session and session.get("waiting_for_timer"):

        if not text.isdigit():
            await update.message.reply_text("❌ Please enter a number between 5 and 600.")
            return

        seconds = int(text)

        if seconds < 5 or seconds > 600:
            await update.message.reply_text("❌ Time must be between 5 and 600 seconds.")
            return

        session["time_per_question"] = seconds
        session["waiting_for_timer"] = False

        await update.message.reply_text(
            f"✅ Timer set: {seconds} seconds per question.\nStarting now..."
        )

        await send_next_question(user_id, context)
        return

    # ---------------- Quiz Name Step ----------------
    if user_id in temp_uploads:
        quiz_name = text
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
        return


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
        "questions": questions,
        "waiting_for_timer": True,
        "time_per_question": 30
    }

    await query.message.reply_text("⏱️ Enter time per question in seconds (5 - 600):")


async def send_next_question(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = user_sessions.get(user_id)
    if not session:
        return

    idx = session["index"]
    questions = session["questions"]
    chat_id = session["chat_id"]
    time_per_question = session.get("time_per_question", 30)

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
        is_anonymous=False,
        open_period=time_per_question
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

    # One text handler for both timer + quiz name
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_handler(CallbackQueryHandler(start_quiz_button, pattern=r"^STARTQUIZ\|"))
    app.add_handler(PollAnswerHandler(poll_answer))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
