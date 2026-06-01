#!/usr/bin/env python3
"""
Битва Умов — викторина с очками для больших Telegram-групп
"""

import asyncio
import html
import logging
import os
import time
import random
from dataclasses import dataclass, field
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.constants import ParseMode

from questions import QUESTIONS

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── Настройки ────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
# Укажи Telegram user_id админов через запятую в переменной ADMIN_IDS
# Например: ADMIN_IDS=123456789,987654321
ADMIN_IDS: set[int] = {767633350}

ANSWER_TIMEOUT = 15       # секунд на ответ
QUESTIONS_PER_GAME = 30   # сколько вопросов в одной игре
BASE_SCORE = 100          # очки за правильный ответ
SPEED_BONUS = 50          # максимальный бонус за скорость
STREAK_BONUS = 20         # бонус за каждый ответ в серии (2+)
PAUSE_BETWEEN = 6         # секунд между вопросами

# ─── Модель данных ────────────────────────────────────────────────────────────
@dataclass
class Player:
    user_id: int
    name: str
    username: str
    score: int = 0
    correct: int = 0
    streak: int = 0     # текущая серия правильных ответов подряд


@dataclass
class GameState:
    phase: str = "idle"          # idle | registration | playing | finished
    chat_id: Optional[int] = None
    players: dict = field(default_factory=dict)    # user_id -> Player
    questions: list = field(default_factory=list)
    q_index: int = 0
    round_answers: dict = field(default_factory=dict)  # user_id -> (ans, timestamp)
    round_start: float = 0.0
    question_msg: Optional[Message] = None
    timer_task: Optional[asyncio.Task] = None


game = GameState()


# ─── Вспомогательные функции ──────────────────────────────────────────────────
async def is_admin(user_id: int, chat_id: int, bot) -> bool:
    if user_id in ADMIN_IDS:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def player_label(player: Player) -> str:
    if player.username:
        return f"@{player.username}"
    return player.name


def player_html(player: Player) -> str:
    if player.username:
        return f"@{html.escape(player.username)}"
    return html.escape(player.name)


def make_keyboard(q_index: int) -> InlineKeyboardMarkup:
    q = game.questions[q_index]
    buttons = [
        InlineKeyboardButton(f"{chr(65 + i)}. {opt}", callback_data=f"a_{q_index}_{i}")
        for i, opt in enumerate(q["opts"])
    ]
    rows = [buttons[i: i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def top_players(n: int = 10) -> list[Player]:
    return sorted(game.players.values(), key=lambda p: p.score, reverse=True)[:n]


# ─── Команды игроков ──────────────────────────────────────────────────────────
async def cmd_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if game.phase != "registration":
        await update.message.reply_text("⛔ Регистрация сейчас закрыта.")
        return

    user = update.effective_user
    if user.id in game.players:
        await update.message.reply_text(
            f"✅ {player_label(Player(user.id, user.first_name, user.username or ''))}, ты уже в списке!"
        )
        return

    game.players[user.id] = Player(
        user_id=user.id,
        name=user.first_name or "Игрок",
        username=user.username or "",
    )
    count = len(game.players)
    await update.message.reply_text(
        f"✅ <b>{html.escape(user.first_name or 'Игрок')}</b> вступил в игру!\n👥 Участников: <b>{count}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_players(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if game.phase == "idle":
        await update.message.reply_text("Игра ещё не начата.")
        return
    count = len(game.players)
    await update.message.reply_text(f"👥 Зарегистрировано участников: <b>{count}</b>", parse_mode=ParseMode.HTML)


async def cmd_score(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показать текущий топ во время игры"""
    if game.phase not in ("playing", "finished"):
        return
    top = top_players(5)
    if not top:
        return
    lines = ["📊 <b>Текущий топ-5:</b>\n"]
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, p in enumerate(top):
        lines.append(f"{medals[i]} {player_html(p)} — <b>{p.score}</b> очков")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎮 <b>БИТВА УМОВ — как играть?</b>\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>ПОРЯДОК ЗАПУСКА ИГРЫ:</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"

        "1️⃣ Администратор пишет /bitva_newgame\n"
        "   → Бот открывает регистрацию\n\n"

        "2️⃣ Все участники пишут /bitva_join\n"
        "   → Каждый записывается в игру\n\n"

        "3️⃣ Администратор пишет /bitva_start\n"
        "   → Игра начинается, появляются вопросы\n\n"

        "4️⃣ На каждый вопрос нажимаете кнопку с ответом\n"
        f"   → У вас {ANSWER_TIMEOUT} секунд на ответ\n\n"

        "5️⃣ После всех вопросов бот объявляет победителей 🏆\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "💰 <b>СИСТЕМА ОЧКОВ:</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"✅ Правильный ответ — <b>{BASE_SCORE} очков</b>\n"
        f"⚡ Бонус за скорость — до <b>+{SPEED_BONUS} очков</b>\n"
        f"🔥 Серия правильных подряд — <b>+{STREAK_BONUS} очков</b> к каждому\n"
        f"❌ Неверный ответ — 0 очков, серия сбрасывается\n\n"

        "🏆 Победители — топ-3 игрока по сумме очков!\n\n"

        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Другие команды:</b>\n"
        "/bitva_players — сколько человек записалось\n"
        "/bitva_score — текущий рейтинг во время игры"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ─── Команды админа ───────────────────────────────────────────────────────────
async def cmd_newgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id, update.effective_chat.id, ctx.bot):
        await update.message.reply_text("⛔ Только для администраторов.")
        return
    if game.phase != "idle":
        await update.message.reply_text("⚠️ Игра уже запущена. Используй /bitva_stop чтобы сбросить.")
        return

    # Сброс и открытие регистрации
    game.phase = "registration"
    game.chat_id = update.effective_chat.id
    game.players.clear()
    game.q_index = 0
    game.questions = []

    await update.message.reply_text(
        "🎮 <b>БИТВА УМОВ</b> — открыта регистрация!\n\n"
        "Нажми /bitva_join чтобы участвовать 👇\n\n"
        f"📝 Всего будет <b>{QUESTIONS_PER_GAME} вопросов</b>\n"
        f"⏱ На каждый вопрос <b>{ANSWER_TIMEOUT} секунд</b>\n\n"
        "🏆 Призы получат топ-3 игрока по очкам!\n\n"
        "<i>Когда все зарегистрируются, администратор напишет /bitva_start</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_startgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id, update.effective_chat.id, ctx.bot):
        await update.message.reply_text("⛔ Только для администраторов.")
        return
    if game.phase != "registration":
        await update.message.reply_text("Сначала открой регистрацию: /bitva_newgame")
        return
    if len(game.players) < 2:
        await update.message.reply_text("Нужно минимум 2 участника!")
        return

    shuffled = QUESTIONS.copy()
    random.shuffle(shuffled)
    game.questions = shuffled[:QUESTIONS_PER_GAME]
    game.q_index = 0
    game.phase = "playing"

    count = len(game.players)
    await update.message.reply_text(
        f"🚀 <b>Игра началась!</b>\n\n"
        f"👥 Участников: <b>{count}</b>\n"
        f"📝 Вопросов: <b>{len(game.questions)}</b>\n\n"
        "Приготовьтесь... Первый вопрос через 5 секунд!",
        parse_mode=ParseMode.HTML,
    )

    await asyncio.sleep(5)
    await ask_question(ctx.application)


async def cmd_stopgame(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id, update.effective_chat.id, ctx.bot):
        await update.message.reply_text("⛔ Только для администраторов.")
        return
    if game.timer_task:
        game.timer_task.cancel()
    game.phase = "idle"
    game.players.clear()
    await update.message.reply_text("🛑 Игра остановлена и сброшена.")


async def cmd_forceresult(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Принудительно завершить игру и показать результаты"""
    if not await is_admin(update.effective_user.id, update.effective_chat.id, ctx.bot):
        return
    if game.phase != "playing":
        await update.message.reply_text("Игра не идёт.")
        return
    if game.timer_task:
        game.timer_task.cancel()
    await finish_game(ctx.application)


# ─── Логика вопросов ──────────────────────────────────────────────────────────
async def ask_question(app):
    if game.phase != "playing":
        return
    if game.q_index >= len(game.questions):
        await finish_game(app)
        return

    game.round_answers = {}
    q = game.questions[game.q_index]
    game.round_start = time.time()

    total_q = len(game.questions)
    progress = f"Вопрос {game.q_index + 1} из {total_q}"
    bar_filled = int((game.q_index / total_q) * 10)
    bar = "▓" * bar_filled + "░" * (10 - bar_filled)

    text = (
        f"❓ *{progress}*  [{bar}]\n\n"
        f"*{q['q']}*\n\n"
        f"⏱ У вас *{ANSWER_TIMEOUT} секунд!*"
    )

    msg = await app.bot.send_message(
        game.chat_id,
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=make_keyboard(game.q_index),
    )
    game.question_msg = msg

    game.timer_task = asyncio.create_task(
        _wait_then_check(app, game.q_index)
    )


async def _wait_then_check(app, q_index: int):
    await asyncio.sleep(ANSWER_TIMEOUT)
    if game.phase == "playing" and game.q_index == q_index:
        await process_answers(app)


async def process_answers(app):
    if game.phase != "playing":
        return

    q = game.questions[game.q_index]
    correct_idx = q["ans"]
    correct_letter = chr(65 + correct_idx)
    correct_text = q["opts"][correct_idx]

    answered_count = 0
    correct_count = 0

    for player in game.players.values():
        if player.user_id in game.round_answers:
            ans, ans_time = game.round_answers[player.user_id]
            answered_count += 1
            if ans == correct_idx:
                elapsed = ans_time - game.round_start
                speed_ratio = max(0.0, 1.0 - elapsed / ANSWER_TIMEOUT)
                speed_pts = int(SPEED_BONUS * speed_ratio)
                player.streak += 1
                streak_pts = STREAK_BONUS * (player.streak - 1) if player.streak > 1 else 0
                player.score += BASE_SCORE + speed_pts + streak_pts
                player.correct += 1
                correct_count += 1
            else:
                player.streak = 0
        else:
            player.streak = 0

    # Убрать клавиатуру с истекшего вопроса
    try:
        await app.bot.edit_message_reply_markup(
            chat_id=game.chat_id,
            message_id=game.question_msg.message_id,
            reply_markup=None,
        )
    except Exception:
        pass

    total = len(game.players)
    skipped = total - answered_count

    correct_text_safe = html.escape(correct_text)
    result_text = (
        f"✅ <b>Правильный ответ: {correct_letter}. {correct_text_safe}</b>\n\n"
        f"👥 Ответили: {answered_count}/{total} • "
        f"✔️ Верно: {correct_count} • "
        f"⏭ Пропустили: {skipped}"
    )

    if correct_count > 0:
        fastest = min(
            (uid for uid, (a, _) in game.round_answers.items()
             if a == correct_idx and uid in game.players),
            key=lambda uid: game.round_answers[uid][1],
            default=None,
        )
        if fastest:
            fp = game.players[fastest]
            result_text += f"\n\n⚡ Быстрее всех: <b>{player_html(fp)}</b>"

    try:
        await app.bot.send_message(
            game.chat_id,
            result_text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"process_answers send error: {e}")
        await app.bot.send_message(game.chat_id, f"✅ Правильный ответ: {correct_letter}. {correct_text}")

    game.q_index += 1
    await asyncio.sleep(PAUSE_BETWEEN)
    await ask_question(app)


# ─── Обработчик кнопок ────────────────────────────────────────────────────────
async def answer_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if game.phase != "playing":
        await query.answer("Игра сейчас не идёт.", show_alert=True)
        return

    user = query.from_user
    if user.id not in game.players:
        await query.answer("Ты не зарегистрирован. Следи за следующей игрой!", show_alert=True)
        return

    parts = query.data.split("_")  # a_qindex_answerindex
    try:
        q_index = int(parts[1])
        ans = int(parts[2])
    except (IndexError, ValueError):
        await query.answer()
        return

    if q_index != game.q_index:
        await query.answer("⏰ Время на этот вопрос вышло!", show_alert=True)
        return

    if user.id in game.round_answers:
        await query.answer("Ты уже ответил на этот вопрос!", show_alert=True)
        return

    game.round_answers[user.id] = (ans, time.time())
    await query.answer("✅ Ответ принят!")

    # Если все участники уже ответили — не ждём таймера
    if len(game.round_answers) >= len(game.players):
        if game.timer_task and not game.timer_task.done():
            game.timer_task.cancel()
        asyncio.create_task(process_answers(app_ref[0]))


# ─── Финал ────────────────────────────────────────────────────────────────────
async def finish_game(app):
    game.phase = "finished"

    winners = top_players(3)
    all_top = top_players(10)

    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 <b>ИГРА ОКОНЧЕНА! РЕЗУЛЬТАТЫ:</b>\n\n"

    for i, p in enumerate(winners):
        m = medals[i]
        text += (
            f"{m} <b>{i + 1} место</b> — {player_html(p)}\n"
            f"   💯 Очков: <b>{p.score}</b> | ✔️ Правильных: {p.correct}/{len(game.questions)}\n\n"
        )

    if len(all_top) > 3:
        text += "📊 <b>Остальной топ-10:</b>\n"
        for i, p in enumerate(all_top[3:], start=4):
            text += f"  {i}. {player_html(p)} — {p.score} очков\n"

    text += (
        f"\n👥 Всего участников: <b>{len(game.players)}</b>\n"
        f"📝 Вопросов сыграно: <b>{game.q_index}</b>"
    )

    try:
        await app.bot.send_message(
            game.chat_id,
            text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"finish_game send error: {e}")
        await app.bot.send_message(game.chat_id, "🏆 Игра окончена! Не удалось отобразить результаты.")

    game.phase = "idle"


# ─── Запуск ───────────────────────────────────────────────────────────────────
app_ref: list = []   # хранит ссылку на app для callback без контекста


def main():
    if BOT_TOKEN == "ВСТАВЬ_ТОКЕН_СЮДА":
        print("❌ Укажи BOT_TOKEN в переменной окружения или прямо в коде!")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app_ref.append(app)

    app.add_handler(CommandHandler("bitva_newgame", cmd_newgame))
    app.add_handler(CommandHandler("bitva_start", cmd_startgame))
    app.add_handler(CommandHandler("bitva_stop", cmd_stopgame))
    app.add_handler(CommandHandler("bitva_result", cmd_forceresult))
    app.add_handler(CommandHandler("bitva_join", cmd_join))
    app.add_handler(CommandHandler("bitva_players", cmd_players))
    app.add_handler(CommandHandler("bitva_score", cmd_score))
    app.add_handler(CommandHandler("bitva_help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CallbackQueryHandler(answer_callback, pattern=r"^a_"))

    print("🤖 Бот запущен! Нажми Ctrl+C для остановки.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
