import os
import re
from docx import Document
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

TOKEN = os.getenv("BOT_TOKEN")

user_quiz_data = {}  # user_id -> {"questions": [], "index": 0, "score": 0}


def parse_docx(file_path):
    doc = Document(file_path)
    full_text = "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])

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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\n\n📌 Send me a DOCX file containing your quiz questions.\n\nAfter that type: /begin"
    )


async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in user_quiz_data or not user_quiz_data[user_id]["questions"]:
        await update.message.reply_text("❌ No quiz loaded. Please send a DOCX file first.")
        return

    user_quiz_data[user_id]["index"] = 0
    user_quiz_data[user_id]["score"] = 0

    await send_next_question(update, context)


async def send_next_question(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    data = user_quiz_data.get(user_id)

    if not data:
        return

    idx = data["index"]
    questions = data["questions"]

    if idx >= len(questions):
        score = data["score"]
        total = len(questions)
        percent = round((score / total) * 100, 1)

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🎉 Quiz Finished!\n\n🏆 Score: {score}/{total}\n📊 Percentage: {percent}%"
        )
        return

    q = questions[idx]

    await context.bot.send_poll(
        chat_id=update.effective_chat.id,
        question=f"Q{idx+1}: {q['question']}",
        options=q["options"],
        type="quiz",
        correct_option_id=q["correct"],
        explanation=f"💡 {q['explanation']}",
        is_anonymous=False
    )

    data["index"] += 1


async def handle_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

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
        await update.message.reply_text("❌ Could not find valid questions in the file. Check formatting.")
        return

    user_quiz_data[user_id] = {"questions": questions, "index": 0, "score": 0}

    await update.message.reply_text(
        f"✅ File received!\n📌 Questions loaded: {len(questions)}\n\nType /begin to start the quiz."
    )


async def poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    answer = update.poll_answer

    data = user_quiz_data.get(user_id)
    if not data:
        return

    question_index = data["index"] - 1
    if question_index < 0:
        return

    q = data["questions"][question_index]

    if answer.option_ids and answer.option_ids[0] == q["correct"]:
        data["score"] += 1


def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("begin", begin))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_doc))
    app.add_handler(MessageHandler(filters.POLL_ANSWER, poll_answer))

    print("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
