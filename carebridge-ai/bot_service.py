"""
WeCareSG - Telegram Bot Service
=======================================
Async Telegram bot (python-telegram-bot v21+) that gives Singapore citizens
a low-friction mobile front door into the WeCareSG triage pipeline.

Conversation flow: the bot walks the citizen through a short guided Q&A
(employment/income, dependents & housing, health needs, other urgent
details). At the end, `triage_engine.analyze_hardship()` normalizes the
answers (colloquial housing terms, medical conditions) and synthesizes a
friendly citizen-facing summary (`patient_summary_markdown`) which is shown
for review. The citizen can Submit, Edit any single answer, or Start Over
before anything is persisted.

Pipeline for every submission: sanitizer.sanitize() -> triage_engine
.analyze_hardship() -> database.save_full_case() -> database.get_case()
-> database.publish_case() (broadcasts to the live web dashboard via SSE).

Supports two run modes, both started from `app.py`:
  - polling  (default, zero-config for local dev): runs on a dedicated
    background thread with its own asyncio event loop.
  - webhook  (production): the Application is initialized on a dedicated
    background event loop; inbound updates arrive via the Flask
    `/telegram/webhook` route and are handed off thread-safely with
    `asyncio.run_coroutine_threadsafe`.
"""

import asyncio
import html
import logging
import re
import threading

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database
import triage_engine
from utils import sanitizer

logger = logging.getLogger("wecaresg.bot")

URGENCY_EMOJI = {"High": "\U0001F534", "Medium": "\U0001F7E1", "Low": "\U0001F7E2"}

# ---------------------------------------------------------------------------
# Markdown -> Telegram-safe HTML
# ---------------------------------------------------------------------------
# `patient_summary_markdown` is shared with the website's own lightweight
# markdown renderer, so it uses GFM-style **bold** / _italic_ / "### " /
# "- " syntax. Telegram's legacy Markdown parse mode chokes on double-
# asterisk bold and on stray underscores that show up in generated content
# (e.g. housing_type codes like "3-room_hdb" would otherwise be misread as
# italics). Converting to HTML sidesteps all of that.
TELEGRAM_MAX_MESSAGE_LENGTH = 3500  # stay comfortably under Telegram's 4096 hard limit


def _inline_md_to_html(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"<i>\1</i>", text)
    return text


def markdown_to_telegram_html(md: str) -> str:
    """Converts the shared patient_summary_markdown into safe Telegram HTML."""
    whole_bold_pattern = re.compile(r"^\*\*(.+)\*\*$")
    out_lines = []
    for raw_line in md.split("\n"):
        stripped = raw_line.strip()
        if stripped.startswith("### "):
            content = html.escape(stripped[4:].strip(), quote=False)
            out_lines.append(f"\n<b>{content.upper()}</b>")
        elif whole_bold_pattern.match(stripped):
            content = html.escape(whole_bold_pattern.match(stripped).group(1), quote=False)
            out_lines.append(f"\n<b>\u25B8 {content}</b>")
        elif stripped.startswith("- "):
            content = html.escape(stripped[2:].strip(), quote=False)
            out_lines.append(f"\u2022 {_inline_md_to_html(content)}")
        elif stripped == "":
            out_lines.append("")
        else:
            out_lines.append(_inline_md_to_html(html.escape(stripped, quote=False)))

    text = "\n".join(out_lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def _split_for_telegram(text: str, limit: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list:
    """Splits long HTML text into Telegram-safe chunks on blank-line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def _send_summary_html(chat_id: int, context: ContextTypes.DEFAULT_TYPE, markdown_text: str, reply_markup=None):
    html_text = markdown_to_telegram_html(markdown_text)
    chunks = _split_for_telegram(html_text)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await context.bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode="HTML",
            reply_markup=reply_markup if is_last else None,
        )

# ---------------------------------------------------------------------------
# Guided intake steps
# ---------------------------------------------------------------------------
STEPS = [
    {
        "key": "income_employment",
        "title": "Employment & Income",
        "prompt": (
            "1\uFE0F\u20E3 *Employment & Income*\n"
            "What's your current job/income situation?\n"
            "_e.g. \u201cretrenched last month, no income\u201d or \u201c$1,800/month cleaner job\u201d_"
        ),
    },
    {
        "key": "household",
        "title": "Dependents & Housing",
        "prompt": (
            "2\uFE0F\u20E3 *Dependents & Housing*\n"
            "How many dependents (kids/elderly) do you support, and what's your housing type?\n"
            "_e.g. \u201c2 kids, 3rm flat\u201d or \u201c5-room executive\u201d_"
        ),
    },
    {
        "key": "health",
        "title": "Medical / Health Needs",
        "prompt": (
            "3\uFE0F\u20E3 *Medical / Health Needs*\n"
            "Any medical conditions needing support?\n"
            "_e.g. \u201cmum needs dialysis\u201d, \u201cheart bypass surgery\u201d, \u201cG6PD\u201d \u2014 or type \u201cnone\u201d_"
        ),
    },
    {
        "key": "urgent_context",
        "title": "Other Urgent Details",
        "prompt": (
            "4\uFE0F\u20E3 *Other Urgent Details*\n"
            "Anything else urgent \u2014 rental arrears, eviction notice, food insecurity, etc.?\n"
            "_or type \u201cnone\u201d_"
        ),
    },
]

# In-memory per-chat state. The bot runs on a single dedicated asyncio event
# loop (see start_polling_in_background / start_webhook_mode), so plain dict
# access here is safe without extra locking.
SESSIONS = {}
LAST_CASE_BY_CHAT = {}

# Separate in-memory state machine for the /login and /signup chat flows.
# A WeCareSG account (created via the web app or /signup here) is REQUIRED
# before any triage feature (/triage, /demo, guided Q&A) can be used.
AUTH_SESSIONS = {}

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _is_logged_in(chat_id) -> bool:
    return database.get_citizen_by_telegram_chat(chat_id) is not None


def _get_citizen(chat_id):
    return database.get_citizen_by_telegram_chat(chat_id)


def _new_session():
    return {"step": 0, "answers": {}, "editing_field": None, "pending_payload": None, "mode": "guided"}


def _combine_answers(answers: dict) -> str:
    parts = [answers.get(step["key"], "").strip() for step in STEPS]
    parts = [p for p in parts if p and p.lower() not in ("none", "no", "n/a", "skip")]
    return ". ".join(parts) if parts else "No details provided."


def _rebuild_pending_payload(session: dict, source_text: str = None):
    session["pending_payload"] = None
    text = source_text if source_text is not None else _combine_answers(session["answers"])
    sanitized = sanitizer.sanitize(text)
    session["pending_payload"] = triage_engine.analyze_hardship(sanitized)
    return session["pending_payload"]


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------
def _format_submission_confirmation(record: dict) -> str:
    """The formal, high-trust closing message shown right after a case is persisted."""
    emoji = URGENCY_EMOJI.get(record.get("urgency", "Low"), "\U0001F7E2")
    return (
        f"{emoji} *{record.get('urgency', 'Low')} Urgency*  \u00b7  Vulnerability Score "
        f"*{record.get('vulnerability_score', 0)}/100*\n\n"
        "\u2705 *Case logged & securely submitted to your assigned Family Service Centre (FSC).*\n\n"
        f"*Case ID:* `{record['case_id']}`\n\n"
        "Use /view_case anytime to see this case again, or /start_new to submit another situation."
    )


def _format_case_recap(record: dict) -> str:
    emoji = URGENCY_EMOJI.get(record.get("urgency", "Low"), "\U0001F7E2")
    lines = [
        f"*Case* `{record['case_id']}`  \u00b7  *Status:* {record.get('status', 'New')}",
        f"{emoji} *{record.get('urgency', 'Low')} Urgency*  \u00b7  Vulnerability Score *{record.get('vulnerability_score', 0)}/100*",
        "",
    ]
    schemes = record["matched_schemes"]
    if schemes:
        lines.append("*Matched Schemes:*")
        for m in schemes[:4]:
            lines.append(f"\u2022 {m['short_name']} \u2014 {m['match_percent']}% match")
    lines.append("")
    lines.append("*Gaps:*")
    for g in record["gaps"][:3]:
        lines.append(f"\u26A0\uFE0F {g}")
    return "\n".join(lines)


def _summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("\u2705 Submit Case", callback_data="confirm_submit")],
            [InlineKeyboardButton("\u270F\uFE0F Edit an Answer", callback_data="confirm_edit")],
            [InlineKeyboardButton("\U0001F504 Start Over", callback_data="confirm_restart")],
        ]
    )


def _post_submit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("\U0001F50D View Case", callback_data="view_case")],
            [InlineKeyboardButton("\U0001F504 Start New", callback_data="start_new_triage")],
        ]
    )


def _edit_field_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"\u270F\uFE0F {step['title']}", callback_data=f"edit_field_{step['key']}")]
        for step in STEPS
    ]
    buttons.append([InlineKeyboardButton("\u2B05\uFE0F Back to Summary", callback_data="back_to_summary")])
    return InlineKeyboardMarkup(buttons)


def _demo_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"{key} \u00b7 {scenario['label']}", callback_data=f"demo_{key}")]
        for key, scenario in triage_engine.DEMO_SCENARIOS.items()
    ]
    return InlineKeyboardMarkup(buttons)


def _auth_required_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("\U0001F510 Log In", callback_data="auth_login")],
            [InlineKeyboardButton("\U0001F4DD Sign Up", callback_data="auth_signup")],
        ]
    )


def _welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("\U0001F9ED Begin Guided Triage", callback_data="begin_triage")],
            [InlineKeyboardButton("\U0001F4CB Try a demo scenario", callback_data="demo_menu")],
        ]
    )


async def _require_login(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if logged in. Otherwise prompts to /login or /signup and
    returns False so the caller can abort the triage action."""
    if _is_logged_in(chat_id):
        return True
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "\U0001F512 *A WeCareSG account is required to use the AI triage assistant.*\n\n"
            "Please /login if you already have an account (including one created on the "
            "website), or /signup to create a new one."
        ),
        parse_mode="Markdown",
        reply_markup=_auth_required_keyboard(),
    )
    return False


async def _send_welcome_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE, citizen: dict):
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"\u2705 Logged in as *{citizen['display_name']}*.\n\n"
            "I'll ask a few quick questions, show you a summary, and you can submit "
            "or edit any answer before it's sent to a case worker."
        ),
        parse_mode="Markdown",
        reply_markup=_welcome_keyboard(),
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    SESSIONS.pop(chat_id, None)
    AUTH_SESSIONS.pop(chat_id, None)

    citizen = _get_citizen(chat_id)
    if citizen:
        await update.message.reply_text(
            f"\U0001F44B *Welcome back to WeCareSG, {citizen['display_name']}!*\n\n"
            "I help match Singapore families to the right assistance schemes \u2014 "
            "ComCare, Silver Support, Medifund, MOE FAS and more.",
            parse_mode="Markdown",
            reply_markup=_welcome_keyboard(),
        )
        return

    await update.message.reply_text(
        "\U0001F44B *Welcome to WeCareSG*\n\n"
        "I help match Singapore families to the right assistance schemes \u2014 "
        "ComCare, Silver Support, Medifund, MOE FAS and more.\n\n"
        "\U0001F512 *An account is required* before I can run triage on your behalf \u2014 "
        "this keeps your case history private and lets you review it on the website too.",
        parse_mode="Markdown",
        reply_markup=_auth_required_keyboard(),
    )


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _begin_login(update.effective_chat.id, context)


async def signup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _begin_signup(update.effective_chat.id, context)


async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_logged_in(chat_id):
        await update.message.reply_text("You're not logged in.")
        return
    database.unlink_telegram_chat(chat_id)
    SESSIONS.pop(chat_id, None)
    await update.message.reply_text("You've been logged out. Send /login to sign back in.")


async def _begin_login(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    if _is_logged_in(chat_id):
        citizen = _get_citizen(chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"You're already logged in as *{citizen['display_name']}*. Send /logout to switch accounts.",
            parse_mode="Markdown",
        )
        return
    AUTH_SESSIONS[chat_id] = {"mode": "login", "step": "username", "data": {}}
    await context.bot.send_message(
        chat_id=chat_id,
        text="\U0001F510 *Log In to WeCareSG*\n\nPlease enter your *username*:",
        parse_mode="Markdown",
    )


async def _begin_signup(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    if _is_logged_in(chat_id):
        citizen = _get_citizen(chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"You're already logged in as *{citizen['display_name']}*.",
            parse_mode="Markdown",
        )
        return
    AUTH_SESSIONS[chat_id] = {"mode": "signup", "step": "display_name", "data": {}}
    await context.bot.send_message(
        chat_id=chat_id,
        text="\U0001F4DD *Create a WeCareSG Account*\n\nWhat's your *full name*?",
        parse_mode="Markdown",
    )


async def _try_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Best-effort deletion of a message containing a password, for basic hygiene."""
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
    except Exception:
        pass


async def _handle_auth_step(update: Update, context: ContextTypes.DEFAULT_TYPE, auth: dict):
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    mode = auth["mode"]
    step = auth["step"]
    data = auth["data"]

    if mode == "login":
        if step == "username":
            data["username"] = text
            auth["step"] = "password"
            await update.message.reply_text("Please enter your *password*:", parse_mode="Markdown")
            return
        if step == "password":
            await _try_delete(update, context)
            citizen = database.verify_citizen_credentials(data["username"], text)
            AUTH_SESSIONS.pop(chat_id, None)
            if not citizen:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="\u274C Invalid username or password. Send /login to try again, or /signup if you don't have an account.",
                )
                return
            database.link_telegram_chat(citizen["id"], chat_id)
            await _send_welcome_menu(chat_id, context, citizen)
            return

    if mode == "signup":
        if step == "display_name":
            data["display_name"] = text
            auth["step"] = "username"
            await update.message.reply_text("Choose a *username*:", parse_mode="Markdown")
            return
        if step == "username":
            data["username"] = text
            auth["step"] = "email"
            await update.message.reply_text("What's your *email address*?", parse_mode="Markdown")
            return
        if step == "email":
            if not EMAIL_PATTERN.match(text):
                await update.message.reply_text("That doesn't look like a valid email. Please try again:")
                return
            data["email"] = text.lower()
            auth["step"] = "password"
            await update.message.reply_text("Choose a *password* (at least 8 characters):", parse_mode="Markdown")
            return
        if step == "password":
            await _try_delete(update, context)
            data["password"] = text
            if len(text) < 8:
                await context.bot.send_message(chat_id=chat_id, text="Password must be at least 8 characters. Try again:")
                return
            auth["step"] = "confirm_password"
            await context.bot.send_message(chat_id=chat_id, text="Confirm your password:")
            return
        if step == "confirm_password":
            await _try_delete(update, context)
            if text != data["password"]:
                auth["step"] = "password"
                await context.bot.send_message(
                    chat_id=chat_id, text="Passwords didn't match. Choose a *password* (at least 8 characters):",
                    parse_mode="Markdown",
                )
                return
            try:
                citizen = database.create_citizen_account(data["username"], data["email"], data["password"], data["display_name"])
            except database.UsernameTakenError:
                auth["step"] = "username"
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Username '{data['username']}' is already taken. Choose a different *username*:",
                    parse_mode="Markdown",
                )
                return
            except database.EmailTakenError:
                auth["step"] = "email"
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"Email '{data['email']}' is already registered. Please enter a different *email address*:",
                    parse_mode="Markdown",
                )
                return
            AUTH_SESSIONS.pop(chat_id, None)
            database.link_telegram_chat(citizen["id"], chat_id)
            await _send_welcome_menu(chat_id, context, citizen)
            return


async def triage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await _require_login(chat_id, context):
        return
    await _begin_triage(chat_id, context)


async def start_new_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await _require_login(chat_id, context):
        return
    await _begin_triage(chat_id, context)


async def demo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not await _require_login(chat_id, context):
        return
    await update.message.reply_text("Pick a demo scenario:", reply_markup=_demo_keyboard())


async def view_case_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_case_recap(update.effective_chat.id, context)


async def _send_case_recap(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    case_id = LAST_CASE_BY_CHAT.get(chat_id)
    if not case_id:
        await context.bot.send_message(
            chat_id=chat_id, text="You haven't submitted a case yet. Send /triage to get started."
        )
        return
    record = database.get_case(case_id)
    if not record:
        await context.bot.send_message(chat_id=chat_id, text="That case could no longer be found.")
        return
    await context.bot.send_message(chat_id=chat_id, text=_format_case_recap(record), parse_mode="Markdown")


async def _begin_triage(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    SESSIONS[chat_id] = _new_session()
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Let's get started.\n\n{STEPS[0]['prompt']}",
        parse_mode="Markdown",
    )


async def _ask_step(chat_id: int, step_index: int, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(chat_id=chat_id, text=STEPS[step_index]["prompt"], parse_mode="Markdown")


async def _show_summary(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = SESSIONS.get(chat_id)
    if not session or not session.get("pending_payload"):
        return
    await _send_summary_html(
        chat_id, context, session["pending_payload"]["patient_summary_markdown"], reply_markup=_summary_keyboard()
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    if data == "auth_login":
        await query.edit_message_text("Let's get you logged in.")
        await _begin_login(chat_id, context)
        return

    if data == "auth_signup":
        await query.edit_message_text("Let's create your account.")
        await _begin_signup(chat_id, context)
        return

    if data == "begin_triage" or data == "start_new_triage":
        if not await _require_login(chat_id, context):
            return
        await query.edit_message_text("Starting your guided triage \u2014 answer each question one at a time.")
        await _begin_triage(chat_id, context)
        return

    if data == "view_case":
        await _send_case_recap(chat_id, context)
        return

    if data == "demo_menu":
        if not await _require_login(chat_id, context):
            return
        await query.edit_message_text("Pick a demo scenario:", reply_markup=_demo_keyboard())
        return

    if data.startswith("demo_"):
        if not await _require_login(chat_id, context):
            return
        key = data.split("_", 1)[1]
        scenario = triage_engine.DEMO_SCENARIOS.get(key)
        if not scenario:
            await query.edit_message_text("Sorry, that demo scenario is no longer available.")
            return
        session = _new_session()
        session["mode"] = "demo"
        session["answers"] = {"urgent_context": scenario["text"]}
        _rebuild_pending_payload(session, source_text=scenario["text"])
        SESSIONS[chat_id] = session
        await query.edit_message_text(f"_Loaded demo scenario:_ \u201c{scenario['text']}\u201d", parse_mode="Markdown")
        await _show_summary(chat_id, context)
        return

    session = SESSIONS.get(chat_id)
    if not session:
        await query.edit_message_text("This session has expired. Send /triage to start again.")
        return

    if data == "confirm_submit":
        payload = session.get("pending_payload")
        if not payload:
            await query.edit_message_text("Nothing to submit yet. Send /triage to start again.")
            return
        citizen = _get_citizen(chat_id)
        if not citizen:
            await query.edit_message_text("Your session expired — please /login again before submitting.")
            return
        case_id = database.save_full_case(payload, channel="telegram", citizen_id=citizen["id"])
        record = database.get_case(case_id)
        database.publish_case(record)
        LAST_CASE_BY_CHAT[chat_id] = case_id
        await query.edit_message_text(_format_submission_confirmation(record), parse_mode="Markdown")
        await context.bot.send_message(
            chat_id=chat_id, text="What would you like to do next?", reply_markup=_post_submit_keyboard()
        )
        SESSIONS.pop(chat_id, None)
        return

    if data == "confirm_edit":
        if session["mode"] == "demo":
            await query.edit_message_text(
                "Demo scenarios can't be edited field-by-field \u2014 starting the guided flow fresh instead."
            )
            await _begin_triage(chat_id, context)
            return
        await query.edit_message_text("Which answer would you like to edit?", reply_markup=_edit_field_keyboard())
        return

    if data == "back_to_summary":
        await _show_summary(chat_id, context)
        return

    if data == "confirm_restart":
        await query.edit_message_text("No problem \u2014 let's start over.")
        await _begin_triage(chat_id, context)
        return

    if data.startswith("edit_field_"):
        field_key = data.split("edit_field_", 1)[1]
        session["editing_field"] = field_key
        step = next(s for s in STEPS if s["key"] == field_key)
        current = session["answers"].get(field_key, "(not answered)")
        await query.edit_message_text(
            f"Current answer for *{step['title']}*:\n_{current}_\n\nSend your updated answer:",
            parse_mode="Markdown",
        )
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    chat_id = update.effective_chat.id

    auth = AUTH_SESSIONS.get(chat_id)
    if auth:
        await _handle_auth_step(update, context, auth)
        return

    session = SESSIONS.get(chat_id)

    if not session:
        if not _is_logged_in(chat_id):
            await update.message.reply_text(
                "\U0001F512 Please /login or /signup first — an account is required to use the AI triage assistant.",
                reply_markup=_auth_required_keyboard(),
            )
            return
        await update.message.reply_text(
            "Send /triage to start a guided intake, /demo to try an example scenario, "
            "or /view_case to see your last submitted case."
        )
        return

    if session.get("editing_field"):
        field_key = session.pop("editing_field")
        session["answers"][field_key] = text
        _rebuild_pending_payload(session)
        await update.message.reply_text("Got it \u2014 updated.")
        await _show_summary(chat_id, context)
        return

    step_index = session["step"]
    session["answers"][STEPS[step_index]["key"]] = text

    if step_index + 1 < len(STEPS):
        session["step"] += 1
        await _ask_step(chat_id, session["step"], context)
        return

    _rebuild_pending_payload(session)
    await _show_summary(chat_id, context)


# ---------------------------------------------------------------------------
# Application bootstrap
# ---------------------------------------------------------------------------
async def handle_triage_error(update, context):
    if isinstance(context.error, triage_engine.TriageError) and update and update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"{context.error}\nNo case was submitted. Send /triage when you are ready to try again.",
        )


def build_application(token: str) -> Application:
    application = Application.builder().token(token).build()
    application.add_error_handler(handle_triage_error)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CommandHandler("signup", signup_command))
    application.add_handler(CommandHandler("logout", logout_command))
    application.add_handler(CommandHandler("triage", triage_command))
    application.add_handler(CommandHandler("demo", demo))
    application.add_handler(CommandHandler("view_case", view_case_command))
    application.add_handler(CommandHandler("start_new", start_new_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return application


_bot_loop = None
_bot_application = None


def start_polling_in_background(token: str) -> threading.Thread:
    """Zero-config local dev mode: long-polling on a dedicated background thread."""

    def _runner():
        asyncio.set_event_loop(asyncio.new_event_loop())
        application = build_application(token)
        logger.info("Telegram bot starting in POLLING mode.")
        # stop_signals=None: signal handlers can only be installed on the main
        # thread, and this bot runs on a background thread alongside Flask.
        application.run_polling(close_loop=False, stop_signals=None)

    thread = threading.Thread(target=_runner, daemon=True, name="telegram-bot-polling")
    thread.start()
    return thread


def start_webhook_mode(token: str, webhook_url: str):
    """Production mode: Application runs on its own event loop thread; Flask's
    /telegram/webhook route hands off inbound updates via
    asyncio.run_coroutine_threadsafe (see submit_webhook_update)."""
    global _bot_loop, _bot_application

    ready = threading.Event()

    def _runner():
        global _bot_loop, _bot_application
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _bot_loop = loop
        _bot_application = build_application(token)

        async def _setup():
            await _bot_application.initialize()
            full_url = f"{webhook_url.rstrip('/')}/telegram/webhook"
            await _bot_application.bot.set_webhook(url=full_url)
            await _bot_application.start()
            logger.info("Telegram bot starting in WEBHOOK mode at %s", full_url)

        loop.run_until_complete(_setup())
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=_runner, daemon=True, name="telegram-bot-webhook")
    thread.start()
    ready.wait(timeout=15)
    return thread


def submit_webhook_update(update_dict: dict):
    """Called from the Flask /telegram/webhook route to hand off an inbound
    update to the bot's dedicated asyncio loop, thread-safely."""
    if not _bot_application or not _bot_loop:
        logger.warning("Received a Telegram webhook update but the bot is not running in webhook mode.")
        return
    update = Update.de_json(update_dict, _bot_application.bot)
    asyncio.run_coroutine_threadsafe(_bot_application.process_update(update), _bot_loop)
