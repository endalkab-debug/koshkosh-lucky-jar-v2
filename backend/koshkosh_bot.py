# -*- coding: utf-8 -*-
"""
KoshKosh Lucky Jar Telegram Bot - PRODUCTION EDITION (WITH 20-MIN DRAW, LIVE WEBAPP LAUNCH & NATURAL PODIUM REVEAL)
"""

import os
import json
import uuid
import random
import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from flask import Flask, jsonify, render_template
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
HOST_TELEGRAM_ID = int(os.getenv("HOST_TELEGRAM_ID", "446176588"))
SUPPORT_CHANNEL = os.getenv("SUPPORT_CHANNEL", "@KoshKoshSupport").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "@KoshKosh_LuckyJar_bot").strip()
CBE_ACCOUNT = os.getenv("CBE_ACCOUNT", "1000211559277")
TELEBIRR_NUMBER = os.getenv("TELEBIRR_NUMBER", "0910823870")
CBEBIRR_NUMBER = os.getenv("CBEBIRR_NUMBER", "0913550415")
PRICE_PER_NUMBER = int(os.getenv("PRICE_PER_NUMBER", "100"))
TOTAL_NUMBERS = int(os.getenv("TOTAL_NUMBERS", "100"))
TRANSACTION_MINUTES = int(os.getenv("TRANSACTION_MINUTES", "15"))
DRAW_DELAY_MINUTES = int(os.getenv("DRAW_DELAY_MINUTES", "20"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PORT = int(os.getenv("PORT", "10000"))
WHEEL_WEBAPP_URL = os.getenv("WHEEL_WEBAPP_URL", "").strip().rstrip("/")

PRIZES = [
    ("🥇 First prize", 4000),
    ("🥈 Second prize", 2000),
    ("🥉 Third prize", 1500),
]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("koshkosh-bot")

flask_app = Flask(__name__)


@flask_app.route("/")
def home():
    return "KoshKosh Lucky Jar Bot is running.", 200


@flask_app.route("/health")
def health():
    return {"status": "ok"}, 200


@flask_app.route("/wheel")
def wheel_page():
    return render_template("wheel.html", total_numbers=TOTAL_NUMBERS)


def get_wheel_snapshot():
    with db() as conn:
        settings_rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN "
            "('wheel_round_id','wheel_status','wheel_active_rank','wheel_reveal_count')"
        ).fetchall()
        settings = {row["key"]: row["value"] for row in settings_rows}
        round_row = conn.execute(
            "SELECT round_number FROM game_state WHERE id=1"
        ).fetchone()
        current_round = f"KLR-{round_row['round_number']:05d}" if round_row else "KLR-00001"
        raw_round = settings.get("wheel_round_id")
        round_id = raw_round if raw_round and raw_round != "None" else current_round
        status = settings.get("wheel_status") or "idle"
        active_rank = int(settings.get("wheel_active_rank") or 0)
        reveal_count = int(settings.get("wheel_reveal_count") or 0)

        rows = []
        if status in ("spinning", "complete") and reveal_count > 0:
            rows = conn.execute(
                "SELECT prize_rank, prize_name, amount, number, drawn_at "
                "FROM draw_results WHERE round_id=%s AND prize_rank <= %s "
                "ORDER BY prize_rank",
                (round_id, reveal_count),
            ).fetchall()

    return {
        "round_id": round_id,
        "status": status,
        "active_rank": active_rank,
        "revealed_count": reveal_count,
        "total_numbers": TOTAL_NUMBERS,
        "results": [dict(row) for row in rows],
    }


@flask_app.route("/api/wheel/state")
def wheel_state_api():
    try:
        return jsonify(get_wheel_snapshot()), 200
    except Exception:
        logger.exception("Wheel state API failed")
        return jsonify({"error": "Wheel state is temporarily unavailable."}), 503


def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ---------------------------------------------------------------------------
# TIME / CONNECTION POOL HELPERS
# ---------------------------------------------------------------------------
def utc_now():
    return datetime.now(timezone.utc)


def to_iso(value):
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value):
    if not value or value == "None":
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


db_pool = None
LANGUAGE_CACHE = {}
LANGUAGE_CACHE_TTL = 300


def init_db_pool():
    global db_pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is missing.")
    db_pool = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=max(2, int(os.getenv("DB_POOL_MAX", "5"))),
        timeout=float(os.getenv("DB_POOL_TIMEOUT", "10")),
        max_idle=float(os.getenv("DB_POOL_MAX_IDLE", "300")),
        kwargs={"row_factory": dict_row, "connect_timeout": 8},
    )


@contextmanager
def db():
    if db_pool is None:
        init_db_pool()
    with db_pool.connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def ensure_user(user_id):
    with db() as conn:
        conn.execute(
            "INSERT INTO users(user_id, created_at) VALUES(%s, %s) ON CONFLICT DO NOTHING",
            (user_id, to_iso(utc_now())),
        )
        row = conn.execute("SELECT language FROM users WHERE user_id=%s", (user_id,)).fetchone()
    language = row["language"] if row else "en"
    LANGUAGE_CACHE[user_id] = (language, time.monotonic())
    return language


def get_language(user_id):
    cached = LANGUAGE_CACHE.get(user_id)
    if cached and time.monotonic() - cached[1] < LANGUAGE_CACHE_TTL:
        return cached[0]
    with db() as conn:
        row = conn.execute(
            "SELECT language FROM users WHERE user_id=%s", (user_id,)
        ).fetchone()
    language = row["language"] if row else "en"
    LANGUAGE_CACHE[user_id] = (language, time.monotonic())
    return language


def set_language(user_id, language):
    ensure_user(user_id)
    with db() as conn:
        conn.execute(
            "UPDATE users SET language=%s WHERE user_id=%s",
            (language, user_id),
        )
    LANGUAGE_CACHE[user_id] = (language, time.monotonic())


def setting(key):
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key=%s", (key,)
        ).fetchone()
    return row["value"] if row else None


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO app_settings(key, value) VALUES(%s, %s) "
            "ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )


def get_current_round():
    with db() as conn:
        row = conn.execute("SELECT round_number FROM game_state WHERE id=1").fetchone()
    round_num = row["round_number"] if row else 1
    return f"KLR-{round_num:05d}"


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                language TEXT NOT NULL DEFAULT 'en',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS numbers (
                number INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'available'
                    CHECK(status IN ('available','reserved','sold')),
                user_id BIGINT,
                cart_token TEXT,
                expires_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS carts (
                user_id BIGINT PRIMARY KEY,
                token TEXT NOT NULL UNIQUE,
                numbers_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'selecting'
                    CHECK(state IN ('selecting','awaiting_name','awaiting_phone',
                                    'awaiting_payment','locked')),
                customer_name TEXT,
                customer_phone TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                cart_token TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                customer_phone TEXT NOT NULL,
                numbers_json TEXT NOT NULL,
                total INTEGER NOT NULL,
                language TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('awaiting_payment','approved','rejected',
                                     'expired','cancelled')),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                channel_message_id BIGINT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS draw_results (
                prize_rank INTEGER PRIMARY KEY,
                round_id TEXT NOT NULL,
                prize_name TEXT NOT NULL,
                amount INTEGER NOT NULL,
                number INTEGER NOT NULL,
                winner_user_id BIGINT NOT NULL,
                drawn_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS game_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                round_number INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'open'
            )
        """)
        conn.execute(
            "INSERT INTO game_state(id, round_number, status) VALUES(1, 1, 'open') ON CONFLICT DO NOTHING"
        )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO numbers(number,status) VALUES(%s, 'available') ON CONFLICT DO NOTHING",
                [(n,) for n in range(1, TOTAL_NUMBERS + 1)],
            )
        conn.execute("INSERT INTO app_settings(key,value) VALUES('draw_at', NULL) ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO app_settings(key,value) VALUES('draw_completed', '0') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO app_settings(key,value) VALUES('warning_sent_5min', '0') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO app_settings(key,value) VALUES('warning_live_sent', '0') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO app_settings(key,value) VALUES('last_fomo_sent', NULL) ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO app_settings(key,value) VALUES('wheel_round_id', NULL) ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO app_settings(key,value) VALUES('wheel_status', 'idle') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO app_settings(key,value) VALUES('wheel_active_rank', '0') ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO app_settings(key,value) VALUES('wheel_reveal_count', '0') ON CONFLICT DO NOTHING")


# ---------------------------------------------------------------------------
# TRANSLATIONS
# ---------------------------------------------------------------------------
LANGUAGES = {
    "en": {
        "welcome": "🎉 Welcome to KoshKosh Lucky Jar!\nRound: {round_id}\n\nChoose an option below.",
        "play": "🎟️ Play / Select Numbers",
        "board": "📊 Number Board",
        "rules": "📜 Rules & Prizes",
        "language": "🌐 Change Language",
        "contact": "📞 Contact Support",
        "back": "🔙 Back to Main Menu",
        "choose_language": "Please choose your language:",
        "language_changed": "✅ Language changed to English.",
        "select_numbers": "🎟️ Select your lucky number(s).\nPrice: {price} Birr per number.\n\n🟢 Available   🟠 Your selection   ⏳ Reserved   🔴 Sold",
        "selected": "Selected: {numbers}\nTotal: {total} Birr",
        "no_selection": "Please select at least one available number.",
        "number_taken": "Number {number} is no longer available.",
        "number_added": "Number {number} added.",
        "number_removed": "Number {number} removed.",
        "checkout": "✅ Continue to Checkout",
        "cancel": "❌ Cancel Selection",
        "name_prompt": "Please enter your full name:",
        "phone_prompt": "Please enter your phone number:",
        "invoice": (
            "🧾 PAYMENT INVOICE\n\nRound: {round_id}\nOrder ID: {order_id}\nName: {name}\n"
            "Phone: {phone}\nNumbers: {numbers}\nTotal: {total} Birr\n\n"
            "Pay to:\nCBE: {cbe}\nTelebirr: 0910823870\nCBEBirr: {cbebirr}\n\n"
            "After payment, send your payment screenshot here in this bot. "
            "Your reservation expires at {expires} UTC unless payment is approved."
        ),
        "expired": "⌛ Your 15-minute reservation has expired. The numbers are available again.",
        "rules_text": (
            "📜 RULES & PRIZES\n\n"
            f"• {PRICE_PER_NUMBER} Birr per number.\n"
            f"• Payment must be completed within {TRANSACTION_MINUTES} minutes.\n"
            "• Numbers become sold only after host verification.\n"
            f"• When all {TOTAL_NUMBERS} numbers are sold, the draw is scheduled "
            f"{DRAW_DELAY_MINUTES} minutes later.\n\n"
            "🏆 PRIZES\n🥇 4,000 ETB\n🥈 2,000 ETB\n🥉 1,500 ETB"
        ),
        "board_text": (
            "📊 NUMBER BOARD\n"
            "🟢 Available: {available}\n"
            "⏳ Reserved: {reserved}\n"
            "🔴 Sold: {sold}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👥 SOLD NUMBERS / CUSTOMERS\n"
            "Full Name       Phone Number      Number\n"
            "{customer_list}"
        ),
        "no_sold_customers": "No sold numbers yet.",
        "main_menu": "Main menu:",
        "support_info": f"Contact our support channel: {SUPPORT_CHANNEL}",
        "host_only": "Only the authorized host can do this.",
        "order_channel": (
            "💳 NEW PAYMENT ORDER\n\nOrder: {order_id}\nName: {name}\n"
            "Phone: {phone}\nUser ID: {telegram_id}\nNumbers: {numbers}\n"
            "Amount: {total} Birr\n\nVerify the payment in the receiving account."
        ),
        "approved_host": "✅ Payment approved.",
        "rejected_host": "❌ Payment rejected.",
        "payment_confirmed": "✅ Your payment has been approved!\nYour sold number(s): {numbers}\nThank you for playing.",
        "payment_rejected": "❌ Your payment was rejected. Please contact support if you believe this is an error.",
        "sold_out": "🎉 All numbers have been sold! The draw for round {round_id} is scheduled for {draw_time} (Ethiopia time).",
        "approved_button": "✅ Approve",
        "rejected_button": "❌ Reject",
        "error_msg": "⚠️ Something went wrong. Please try again or contact support.",
        "locked": "Your selection is being processed. Please finish or cancel the current order first.",
        "screenshot_received": "📸 Your payment screenshot has been sent to the host for verification. Please wait.",
        "send_screenshot": "Please send your payment screenshot as a photo in this chat.",
        "draw_pending": "🎲 The draw is scheduled for {draw_time} Ethiopia time.\nTime remaining: {remaining}.",
        "draw_not_ready": "Numbers are still being sold. The draw will happen 20 minutes after all numbers are sold. Good luck!",
        "draw_results": "🏆 KOSHKOSH LUCKY JAR RESULTS\n\n{results}",
        "cancelled": "Your selection has been cancelled. Reserved numbers have been released.",
        "invalid_phone": "Please enter a valid phone number.",
        "invalid_name": "Please enter a valid full name.",
        "warning_5min": "⏰ Only 5 minutes left! The draw for round {round_id} will start in 5 minutes. Get ready! 🎰",
        "draw_info": "🎲 Draw Time",
        "history_info": "📜 Game History",
        "history_title": "📜 **YOUR HISTORY**",
        "history_tickets": "🎟️ Recently Bought Ticket Numbers",
        "history_my_wins": "🏆 Prizes You Won:",
        "history_global_wins": "🌍 Previous Round Global Winners History:",
        "history_ticket_line": "• Order {order_id} | Numbers: {numbers} | {total} ETB | {date}",
        "history_no_tickets": "• No tickets bought yet.",
        "history_win_line": "• {prize} ({amount:,} ETB) | Number: {number} | {date}",
        "history_winner_info": "  👤 {name} | 📞 {phone}",
        "history_no_wins": "• No wins yet.",
        "history_no_global_wins": "• No global wins yet.",
        "wheel_btn": "Watch Live Draw",
        "wheel_launch_msg": (
            "🎡 **KOSHKOSH LUCKY WHEEL IS LIVE!**\n\n"
            "The draw is starting right now! Open the wheel to watch all the winners revealed live.\n"
            "👇👇👇"
        ),
        "winner_private_msg": (
            "🏆 Congratulations! You won a KoshKosh Lucky Jar prize!\n\n"
            "{rank_emoji} {prize_name} — {amount:,} ETB\n"
            "Winning Number: {final_number}\n\n"
            "Name: {customer_name}\n"
            "Phone: {customer_phone}\n\n"
            "The host will contact you for your payout."
        ),
        "host_payout_list": "🏆 KOSHKOSH LUCKY JAR — 3 WINNERS PAYOUT LIST:\n\n{winner_details}",
    },
    "am": {
        "welcome": "🎉 እንኳን ወደ ኮሽኮሽ ሎተሪ በደህና መጡ!\nዙር፦ {round_id}\n\nከታች ከቀረቡት አማራጮች ውስጥ ይምረጡ።",
        "play": "🎟️ ይጫወቱ / ቁጥር ይምረጡ",
        "board": "📊 የቁጥሮች ሰንጠረዥ",
        "rules": "📜 ደንቦች እና ሽልማቶች",
        "language": "🌐 ቋንቋ ይቀይሩ",
        "contact": "📞 የድጋፍ አገልግሎት",
        "back": "🔙 ወደ ዋናው ማውጫ",
        "choose_language": "እባክዎ ቋንቋዎን ይምረጡ፦",
        "language_changed": "✅ ቋንቋው ወደ አማርኛ ተቀይሯል።",
        "select_numbers": "🎟️ የዕድል ቁጥርዎን ይምረጡ።\nየአንድ ዕጣ ቁጥር ዋጋ ፦ {price} ብር\n\n🟢 ያልተያዘ 🟠 የመረጡት ⏳ የተያዘ 🔴 የተሸጠ",
        "selected": "የመረጡት የዕጣ ቁጥሮች፦ {numbers}\nጠቅላላ ዋጋ ፦ {total} ብር",
        "no_selection": "እባክዎ ቢያንስ አንድ ክፍት ቁጥር ይምረጡ።",
        "number_taken": "ቁጥር {number} ከእንግዲህ ክፍት አይደለም።",
        "number_added": "ቁጥር {number} ተጨምሯል።",
        "number_removed": "ቁጥር {number} ተወግዷል።",
        "checkout": "✅ ወደ ክፍያ ይቀጥሉ",
        "cancel": "❌ ምርጫዎትን ይሰርዙ",
        "name_prompt": "እባክዎ ሙሉ ስምዎን ያስገቡ፦",
        "phone_prompt": "እባክዎ ስልክ ቁጥርዎን ያስገቡ፦",
        "invoice": "🧾 የክፍያ ደረሰኝ\n\nዙር፦ {round_id}\nየትዕዛዝ መለያ፦ {order_id}\nሙሉ ስም፦ {name}\nስልክ ቁጥር፦ {phone}\nየዕጣ ቁጥሮች፦ {numbers}\nጠቅላላ ዋጋ፦ {total} ብር\n\nየክፍያ አማራጮች፦\nCBE፦ {cbe}\nTelebirr፦ 0910823870\nCBEBirr፦ {cbebirr}\n\nየከፈሉበትን የክፍያ ደረሰኝ (Screenshot) ያስገቡ።\nለመረጡት የዕጣ ቁጥሮችዎ ክፍያ ፈፅመው ደረሰኝዎን እስከሚልኩ ድረስ ለ15 ደቂቃዎች የመረጡት የዕጣ ቁጥሮችዎ ተይዘውልዎታል።",
        "expired": "⌛ የ15 ደቂቃ የቁጥር ይዞታ ጊዜ አልቋል። ቁጥሮቹ እንደገና ክፍት ሆነዋል።",
        "rules_text": "📜 ደንቦች እና ሽልማቶች\n\n• የአንድ የዕጣ ቁጥር ዋጋ {price} ብር ነው።\n• ለመረጡት የዕጣ ቁጥሮች ክፍያዎትን በ{minutes} ደቂቃ ውስጥ መፈጸም አለበት።\n• ክፍያው በአጫዋቹ ከተረጋገጠ በኋላ የዕጣ ቁጥሮቹ በእርስዎ ስም ዕጣው ውስጥ ይመዘገባሉ።\n• ለሽያጭ የቀረቡት ሁሉም ቁጥሮች ተሽጠው እንዳለቁ ዕጣው ከ{delay} ደቂቃ በኋላ ይወጣል።\n\n🏆 ሽልማቶች\n🥇 4,000 ብር\n🥈 2,000 ብር\n🥉 1,500 ብር",
        "board_text": (
            "📊 የቁጥሮች ሁኔታ\n"
            "🟢 ክፍት፦ {available}\n"
            "⏳ የተያዙ፦ {reserved}\n"
            "🔴 የተሸጡ፦ {sold}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "👥 የተሸጡ ቁጥሮች ደንበኞች\n"
            "ሙሉ ስም          ስልክ ቁጥር       ቁጥር\n"
            "{customer_list}"
        ),
        "no_sold_customers": "እስካሁን ምንም የተሸጠ ቁጥር የለም።",
        "main_menu": "ዋና ማውጫ፦",
        "support_info": f"ለድጋፍ ይህንን ቻናል ይጠቀሙ፦ {SUPPORT_CHANNEL}",
        "host_only": "ይህንን ተግባር አስተናጋጁ ብቻ ማከናወን ይችላል።",
        "order_channel": (
            "💳 አዲስ የክፍያ ትዕዛዝ\n\nትዕዛዝ፦ {order_id}\nስም፦ {name}\n"
            "ስልክ፦ {phone}\nUser ID፦ {telegram_id}\nቁጥሮች፦ {numbers}\n"
            "መጠን፦ {total} ብር\n\nክፍያውን በተቀባይ ሂሳብ ያረጋግጡ።"
        ),
        "approved_host": "✅ ክፍያው ጸድቋል።",
        "rejected_host": "❌ ክፍያው ውድቅ ተደርጓል።",
        "payment_confirmed": "✅ ክፍያዎት ተረጋግጧል!\nየገዙት የዕጣ ቁጥሮች፦ {numbers}\nእናመሰግናለን።",
        "payment_rejected": "❌ ክፍያዎ ውድቅ ተደርጓል። ማንኛውም ችግር ካለ በዚህ @koshkoshsupport ድጋፍን ያግኙ።",
        "sold_out": "🎉 ሁሉም ቁጥሮች ተመርጠው አልቀዋል! የ {round_id} ዙር ዕጣ በ {draw_time} ላይ ይወጣል።",
        "approved_button": "✅ አጽድቅ",
        "rejected_button": "❌ ውድቅ አድርግ",
        "error_msg": "⚠️ ችግር ተፈጥሯል። እባክዎ እንደገና ይሞክሩ።",
        "locked": "የመረጡት ቁጥሮች በሂደት ላይ ናቸው። እባክዎ የአሁኑን ትዕዛዝ ይጨርሱ ወይም ይሰርዙ።",
        "screenshot_received": "✅ ጥያቄዎን ለማጽደቅ ወደ አጫዋቹ ተልኳል። ክፍያዎት በትክክል መፈፀሙን በአጫዋቹ በኩል ተረጋግጦ እንደፀደቀ የማረጋገጫ መለዕክት ይደርስዎታል።",
        "send_screenshot": "እባክዎ የክፍያ ስክሪንሾትዎን በፎቶ ይላኩ።",
        "draw_pending": "🎲 ዕጣው {draw_time} ላይ ይወጣል።\nቀሪ ጊዜ፦ {remaining}።",
        "draw_not_ready": "ቁጥሮች ገና በመሸጥ ላይ ናቸው፣ ሁሉም ቁጥሮች ተሽጠው እንዳለቁ ከ20 ደቂቃዎች በኋላ ዕጣው ይወጣል። መልካም ዕድል!",
        "draw_results": "🏆 የኮሽኮሽ ዕጣ ውጤት\n\n{results}",
        "cancelled": "ምርጫዎ ተሰርዟል። የተያዙ ቁጥሮች እንደገና ክፍት ሆነዋል።",
        "invalid_phone": "እባክዎ ትክክለኛ ስልክ ቁጥር ያስገቡ።",
        "invalid_name": "እባክዎ ትክክለኛ ሙሉ ስም ያስገቡ።",
        "warning_5min": "የ {round_id} ዙር ዕጣ ሊወጣ ⏰ 5 ደቂቃ ብቻ ቀረው! ዕጣው በ5 ደቂቃ ውስጥ ይወጣል። ይዘጋጁ! 🎰",
        "draw_info": "🎲 የዕጣ ሰዓት",
        "history_info": "📜 ጨዋታ ታሪክ",
        "history_title": "📜 **የጨዋታ ታሪክዎ**",
        "history_tickets": "🎟️ በቅርብ የገዟቸው የዕጣ ቁጥሮች",
        "history_my_wins": "🏆 እርስዎ ያሸነፏቸው ሽልማቶች:",
        "history_global_wins": "🌍 ያለፈው ዙር አጠቃላይ የአሸናፊዎች ታሪክ",
        "history_ticket_line": "• ትዕዛዝ {order_id} | ቁጥሮች: {numbers} | {total} ብር | {date}",
        "history_no_tickets": "• እስካሁን ምንም የተገዛ የዕጣ ቁጥር የለም።",
        "history_win_line": "• {prize} ({amount:,} ብር) | ቁጥር: {number} | {date}",
        "history_winner_info": "  👤 {name} | 📞 {phone}",
        "history_no_wins": "• እስካሁን ምንም ያሸነፉት የለም።",
        "history_no_global_wins": "• አሁን ድረስ አለምአቀፍ አሸናፊ የለም።",
        "wheel_btn": "ቀጥታ የዕጣ አወጣጥ ይመልከቱ",
        "wheel_launch_msg": (
            "🎡 **የኮሽኮሽ ዕጣ አወጣጥ ስነስርዐት በሂደት ላይ ነው!**\n\n"
            "የዕጣ አወጣጥ ስነስርዐት በቀጥታ ለመከታተል አሁኑኑ የዕጣ መንኮራኩሩን ይክፈቱ!\n"
            "👇👇👇"
        ),
        "winner_private_msg": (
            "🏆 እንኳን ደስ አለዎት! የኮሽኮሽ ዕጣ ሽልማት አሸናፊ ሆናለዋል!\n\n"
            "{rank_emoji} {prize_name} — {amount:,} ብር\n"
            "የዕድል ቁጥር: {final_number}\n\n"
            "ስም: {customer_name}\n"
            "ስልክ ቁጥር: {customer_phone}\n\n"
            "ሽልማትዎን ለማስረከብ አጫዋቹ ያነጋግርዎታል።"
        ),
        "host_payout_list": "🏆 የኮሽኮሽ ሎተሪ — 3 አሸናፊዎች የክፍያ ዝርዝር:\n\n{winner_details}",
    },
}


def tr(user_id, key, **kwargs):
    language = get_language(user_id)
    template = LANGUAGES.get(language, LANGUAGES["en"]).get(
        key, LANGUAGES["en"].get(key, key)
    )
    return template.format(**kwargs) if kwargs else template


def language_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
            InlineKeyboardButton("🇪🇹 አማርኛ", callback_data="lang:am"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="menu")],
    ])


def wheel_launch_keyboard(user_id="en"):
    if not WHEEL_WEBAPP_URL:
        return None
    btn_text = "Watch Live Draw" if get_language(user_id) == "en" else "ቀጥታ የዕጣ አወጣጥ ይመልከቱ"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            btn_text,
            web_app=WebAppInfo(url=WHEEL_WEBAPP_URL),
        )
    ]])


def main_menu_keyboard(user_id):
    rows = [
        [InlineKeyboardButton(tr(user_id, "play"), callback_data="play")],
        [
            InlineKeyboardButton(tr(user_id, "board"), callback_data="board"),
            InlineKeyboardButton(tr(user_id, "draw_info"), callback_data="draw_info"),
        ],
        [
            InlineKeyboardButton(tr(user_id, "rules"), callback_data="rules"),
            InlineKeyboardButton(tr(user_id, "language"), callback_data="language"),
        ],
        [
            InlineKeyboardButton(tr(user_id, "history_info"), callback_data="history"),
            InlineKeyboardButton(tr(user_id, "contact"), callback_data="support"),
        ],
    ]
    if WHEEL_WEBAPP_URL:
        wheel_label = "Watch Live Draw" if get_language(user_id) == "en" else "ቀጥታ የዕጣ አወጣጥ ይመልከቱ"
        rows.insert(1, [InlineKeyboardButton(
            f"🎡 {wheel_label}",
            web_app=WebAppInfo(url=WHEEL_WEBAPP_URL),
        )])
    return InlineKeyboardMarkup(rows)


def back_keyboard(user_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(tr(user_id, "back"), callback_data="menu")
    ]])


def approval_keyboard(order_id, language):
    labels = LANGUAGES.get(language, LANGUAGES["en"])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(labels["approved_button"], callback_data=f"approve:{order_id}"),
        InlineKeyboardButton(labels["rejected_button"], callback_data=f"reject:{order_id}"),
    ]])


# ---------------------------------------------------------------------------
# RESERVATION / ORDER LOGIC & CACHING
# ---------------------------------------------------------------------------
def get_cart(user_id, context=None):
    if context and "cart_cache" in context.user_data:
        cart = context.user_data["cart_cache"]
        if cart and cart.get("expires") and cart["expires"] > utc_now():
            return cart
    with db() as conn:
        row = conn.execute("SELECT * FROM carts WHERE user_id=%s", (user_id,)).fetchone()
    if not row:
        if context:
            context.user_data["cart_cache"] = None
        return None
    result = dict(row)
    result["numbers"] = json.loads(result["numbers_json"])
    result["expires"] = from_iso(result["expires_at"])
    if not result["expires"] or result["expires"] <= utc_now():
        if context:
            context.user_data["cart_cache"] = None
        return None
    if context:
        context.user_data["cart_cache"] = result
    return result


def get_active_order(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE user_id=%s AND status='awaiting_payment' "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def release_expired_reservations():
    now = to_iso(utc_now())
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7410920261109)")
        expired = conn.execute(
            "SELECT user_id,token FROM carts WHERE expires_at<=%s", (now,)
        ).fetchall()
        for cart in expired:
            conn.execute(
                "UPDATE numbers SET status='available',user_id=NULL,cart_token=NULL,expires_at=NULL "
                "WHERE status='reserved' AND cart_token=%s",
                (cart["token"],),
            )
            conn.execute(
                "UPDATE orders SET status='expired' WHERE cart_token=%s AND status='awaiting_payment'",
                (cart["token"],),
            )
            conn.execute("DELETE FROM carts WHERE user_id=%s", (cart["user_id"],))


def get_number_counts():
    with db() as conn:
        rows = conn.execute(
            "SELECT status,COUNT(*) AS count FROM numbers GROUP BY status"
        ).fetchall()
    counts = {"available": 0, "reserved": 0, "sold": 0}
    for row in rows:
        counts[row["status"]] = row["count"]
    return counts


def get_cached_board_data(context, start_num, end_num):
    now_t = time.monotonic()
    cache = context.bot_data.get("board_cache") if context else None
    page_key = (int(start_num), int(end_num))
    if (
        cache
        and now_t - cache.get("timestamp", 0) < 3
        and isinstance(cache.get("pages"), dict)
    ):
        cached_page = cache["pages"].get(page_key)
        if cached_page:
            return cached_page["counts"], cached_page["statuses"], cached_page.get("sold_customers", [])
    else:
        cache = {"timestamp": now_t, "pages": {}}
    with db() as conn:
        rows = conn.execute(
            """
            WITH counts AS (
                SELECT
                    COUNT(*) FILTER (WHERE status='available')::int AS available,
                    COUNT(*) FILTER (WHERE status='reserved')::int AS reserved,
                    COUNT(*) FILTER (WHERE status='sold')::int AS sold
                FROM numbers
            ), page AS (
                SELECT number, status, user_id
                FROM numbers
                WHERE number BETWEEN %s AND %s
                ORDER BY number
            )
            SELECT page.number, page.status, page.user_id,
                   counts.available, counts.reserved, counts.sold
            FROM counts
            LEFT JOIN page ON TRUE
            ORDER BY page.number
            """,
            (start_num, end_num),
        ).fetchall()
        sold_customers = conn.execute(
            """
            SELECT customer_name, customer_phone, numbers_json, created_at
            FROM orders
            WHERE status = 'approved'
            ORDER BY created_at DESC
            """
        ).fetchall()
    counts = {"available": 0, "reserved": 0, "sold": 0}
    statuses = {}
    if rows:
        counts = {
            "available": rows[0]["available"],
            "reserved": rows[0]["reserved"],
            "sold": rows[0]["sold"],
        }
        for row in rows:
            if row["number"] is not None:
                statuses[row["number"]] = {
                    "number": row["number"],
                    "status": row["status"],
                    "user_id": row["user_id"],
                }
    processed_customers = []
    seen_numbers = set()
    for sc in sold_customers:
        try:
            nums = json.loads(sc["numbers_json"])
        except Exception:
            nums = []
        valid_nums = [n for n in nums if n not in seen_numbers]
        if valid_nums:
            for n in valid_nums:
                seen_numbers.add(n)
            processed_customers.append({
                "customer_name": sc["customer_name"],
                "customer_phone": sc["customer_phone"],
                "numbers": sorted(valid_nums),
            })
    if context:
        if not cache or now_t - cache.get("timestamp", 0) >= 3:
            cache = {"timestamp": now_t, "pages": {}}
        cache["pages"][page_key] = {"counts": counts, "statuses": statuses, "sold_customers": processed_customers}
        context.bot_data["board_cache"] = cache
    return counts, statuses, processed_customers


def get_draw_time():
    return from_iso(setting("draw_at"))


def format_ethiopia_time(value):
    local = value.astimezone(timezone(timedelta(hours=3)))
    return local.strftime("%Y-%m-%d %H:%M:%S")


def remaining_text(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes}m {secs}s"


def number_grid_keyboard(user_id, cart, statuses, page=1):
    selected = set(cart["numbers"]) if cart and cart.get("numbers") else set()
    rows, row = [], []

    per_page = 50
    start_num = (page - 1) * per_page + 1
    end_num = min(page * per_page, TOTAL_NUMBERS)

    for number in range(start_num, end_num + 1):
        status = statuses.get(number, {"status": "available"})
        if number in selected:
            label, callback = f"🟠{number}", f"toggle:{number}:{page}"
        elif status["status"] == "sold":
            label, callback = f"🔴{number}", "noop"
        elif status["status"] == "reserved":
            label, callback = f"⏳{number}", "noop"
        else:
            label, callback = f"🟢{number}", f"toggle:{number}:{page}"

        row.append(InlineKeyboardButton(label, callback_data=callback))

        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page:{page - 1}"))
    if end_num < TOTAL_NUMBERS:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"page:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    if selected:
        rows.append([
            InlineKeyboardButton(tr(user_id, "checkout"), callback_data="checkout"),
            InlineKeyboardButton(tr(user_id, "cancel"), callback_data="cancel"),
        ])

    rows.append([InlineKeyboardButton(tr(user_id, "back"), callback_data="menu")])
    return InlineKeyboardMarkup(rows)


async def show_number_grid(target, user_id, context=None, page=1):
    counts, statuses_slice, _ = await asyncio.to_thread(
        get_cached_board_data, context, 1, TOTAL_NUMBERS
    )
    cart = await asyncio.to_thread(get_cart, user_id, context)

    message = tr(user_id, "select_numbers", price=PRICE_PER_NUMBER)
    message += f"\n\n📄 Page {page} of 2 (Numbers {(page-1)*50 + 1}–{min(page*50, TOTAL_NUMBERS)})"

    if cart and cart.get("numbers"):
        numbers = sorted(cart["numbers"])
        seconds = (cart["expires"] - utc_now()).total_seconds()
        message += "\n\n" + tr(
            user_id, "selected",
            numbers=", ".join(map(str, numbers)),
            total=len(numbers) * PRICE_PER_NUMBER,
        )
        message += f"\n\n⏳ {remaining_text(seconds)}"

    markup = number_grid_keyboard(user_id, cart, statuses_slice, page=page)
    if hasattr(target, "edit_text"):
        await target.edit_text(message, reply_markup=markup)
    else:
        await target.reply_text(message, reply_markup=markup)


def toggle_number_transaction(user_id, number):
    now = utc_now()
    now_iso = to_iso(now)
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7410920261109)")
        cart = conn.execute("SELECT * FROM carts WHERE user_id=%s", (user_id,)).fetchone()
        if cart and from_iso(cart["expires_at"]) <= now:
            conn.execute(
                "UPDATE numbers SET status='available',user_id=NULL,cart_token=NULL,expires_at=NULL "
                "WHERE status='reserved' AND cart_token=%s",
                (cart["token"],),
            )
            conn.execute(
                "UPDATE orders SET status='expired' WHERE cart_token=%s AND status='awaiting_payment'",
                (cart["token"],),
            )
            conn.execute("DELETE FROM carts WHERE user_id=%s", (user_id,))
            cart = None
        if cart and cart["state"] in ("awaiting_name", "awaiting_phone", "awaiting_payment", "locked"):
            counts = conn.execute("SELECT status,COUNT(*) AS count FROM numbers GROUP BY status").fetchall()
            c_dict = {"available": 0, "reserved": 0, "sold": 0}
            for r in counts:
                c_dict[r["status"]] = r["count"]
            return "locked", None, c_dict, cart
        row = conn.execute("SELECT * FROM numbers WHERE number=%s", (number,)).fetchone()
        if not row:
            counts = conn.execute("SELECT status,COUNT(*) AS count FROM numbers GROUP BY status").fetchall()
            c_dict = {"available": 0, "reserved": 0, "sold": 0}
            for r in counts:
                c_dict[r["status"]] = r["count"]
            return "invalid", None, c_dict, cart
        if row["status"] == "reserved" and row["user_id"] == user_id and cart:
            numbers = json.loads(cart["numbers_json"])
            if number in numbers:
                numbers.remove(number)
                conn.execute(
                    "UPDATE numbers SET status='available',user_id=NULL,cart_token=NULL,expires_at=NULL "
                    "WHERE number=%s AND user_id=%s AND cart_token=%s",
                    (number, user_id, cart["token"]),
                )
                if not numbers:
                    conn.execute("DELETE FROM carts WHERE user_id=%s", (user_id,))
                    updated_cart = None
                else:
                    conn.execute(
                        "UPDATE carts SET numbers_json=%s WHERE user_id=%s",
                        (json.dumps(numbers), user_id),
                    )
                    updated_cart_row = conn.execute("SELECT * FROM carts WHERE user_id=%s", (user_id,)).fetchone()
                    updated_cart = dict(updated_cart_row)
                    updated_cart["numbers"] = json.loads(updated_cart["numbers_json"])
                    updated_cart["expires"] = from_iso(updated_cart["expires_at"])
                counts = conn.execute("SELECT status,COUNT(*) AS count FROM numbers GROUP BY status").fetchall()
                c_dict = {"available": 0, "reserved": 0, "sold": 0}
                for r in counts:
                    c_dict[r["status"]] = r["count"]
                return "removed", numbers, c_dict, updated_cart
        if row["status"] != "available":
            counts = conn.execute("SELECT status,COUNT(*) AS count FROM numbers GROUP BY status").fetchall()
            c_dict = {"available": 0, "reserved": 0, "sold": 0}
            for r in counts:
                c_dict[r["status"]] = r["count"]
            return "taken", None, c_dict, cart
        if not cart:
            token = uuid.uuid4().hex
            expires = to_iso(now + timedelta(minutes=TRANSACTION_MINUTES))
            numbers = [number]
            conn.execute(
                "INSERT INTO carts(user_id,token,numbers_json,created_at,expires_at,state) "
                "VALUES(%s,%s,%s,%s,%s,'selecting')",
                (user_id, token, json.dumps(numbers), now_iso, expires),
            )
        else:
            token = cart["token"]
            expires = cart["expires_at"]
            numbers = json.loads(cart["numbers_json"])
            numbers.append(number)
            conn.execute(
                "UPDATE carts SET numbers_json=%s WHERE user_id=%s",
                (json.dumps(numbers), user_id),
            )
        reservation_update = conn.execute(
            "UPDATE numbers SET status='reserved',user_id=%s,cart_token=%s,expires_at=%s "
            "WHERE number=%s AND status='available'",
            (user_id, token, expires, number),
        )
        if reservation_update.rowcount == 0:
            counts = conn.execute("SELECT status,COUNT(*) AS count FROM numbers GROUP BY status").fetchall()
            c_dict = {"available": 0, "reserved": 0, "sold": 0}
            for r in counts:
                c_dict[r["status"]] = r["count"]
            return "taken", None, c_dict, cart
        updated_cart_row = conn.execute("SELECT * FROM carts WHERE user_id=%s", (user_id,)).fetchone()
        updated_cart = dict(updated_cart_row)
        updated_cart["numbers"] = json.loads(updated_cart["numbers_json"])
        updated_cart["expires"] = from_iso(updated_cart["expires_at"])
        counts = conn.execute("SELECT status,COUNT(*) AS count FROM numbers GROUP BY status").fetchall()
        c_dict = {"available": 0, "reserved": 0, "sold": 0}
        for r in counts:
            c_dict[r["status"]] = r["count"]
        return "added", numbers, c_dict, updated_cart


def cancel_user_cart(user_id):
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7410920261109)")
        cart = conn.execute("SELECT token FROM carts WHERE user_id=%s", (user_id,)).fetchone()
        if cart:
            conn.execute(
                "UPDATE numbers SET status='available',user_id=NULL,cart_token=NULL,expires_at=NULL "
                "WHERE status='reserved' AND cart_token=%s", (cart["token"],)
            )
            conn.execute(
                "UPDATE orders SET status='cancelled' WHERE cart_token=%s AND status='awaiting_payment'",
                (cart["token"],),
            )
            conn.execute("DELETE FROM carts WHERE user_id=%s", (user_id,))


def begin_checkout(user_id):
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7410920261109)")
        cart = conn.execute("SELECT * FROM carts WHERE user_id=%s", (user_id,)).fetchone()
        if not cart:
            return "missing"
        if from_iso(cart["expires_at"]) <= utc_now():
            return "expired"
        if cart["state"] != "selecting":
            return "locked"
        existing = conn.execute(
            "SELECT 1 FROM orders WHERE user_id=%s AND status='awaiting_payment' LIMIT 1",
            (user_id,),
        ).fetchone()
        if existing:
            return "locked"
        conn.execute(
            "UPDATE carts SET state='awaiting_name' WHERE user_id=%s", (user_id,)
        )
        return "ok"


def save_customer_name(user_id, name):
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7410920261109)")
        cart = conn.execute("SELECT * FROM carts WHERE user_id=%s", (user_id,)).fetchone()
        if not cart or from_iso(cart["expires_at"]) <= utc_now():
            return "expired"
        if cart["state"] != "awaiting_name":
            return "wrong_state"
        conn.execute(
            "UPDATE carts SET customer_name=%s,state='awaiting_phone' WHERE user_id=%s",
            (name, user_id),
        )
        return "ok"


def create_order(user_id, phone):
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7410920261109)")
        cart = conn.execute("SELECT * FROM carts WHERE user_id=%s", (user_id,)).fetchone()
        if not cart or from_iso(cart["expires_at"]) <= utc_now():
            return None, "expired"
        if cart["state"] != "awaiting_phone":
            return None, "wrong_state"
        existing = conn.execute(
            "SELECT order_id FROM orders WHERE user_id=%s AND status='awaiting_payment'",
            (user_id,),
        ).fetchone()
        if existing:
            return None, "duplicate"
        numbers = json.loads(cart["numbers_json"])
        for number in numbers:
            row = conn.execute(
                "SELECT status,user_id,cart_token FROM numbers WHERE number=%s",
                (number,),
            ).fetchone()
            if not row or row["status"] != "reserved" or row["user_id"] != user_id or row["cart_token"] != cart["token"]:
                return None, "reservation_mismatch"
        order_id = uuid.uuid4().hex[:12].upper()
        language = conn.execute(
            "SELECT language FROM users WHERE user_id=%s", (user_id,)
        ).fetchone()["language"]
        name = cart["customer_name"] or ""
        total = len(numbers) * PRICE_PER_NUMBER
        conn.execute(
            "INSERT INTO orders(order_id,user_id,cart_token,customer_name,customer_phone,"
            "numbers_json,total,language,status,created_at,expires_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                order_id, user_id, cart["token"], name, phone,
                json.dumps(numbers), total, language, "awaiting_payment",
                to_iso(utc_now()), cart["expires_at"],
            ),
        )
        conn.execute(
            "UPDATE carts SET customer_phone=%s,state='awaiting_payment' WHERE user_id=%s",
            (phone, user_id),
        )
        return {
            "order_id": order_id,
            "user_id": user_id,
            "cart_token": cart["token"],
            "name": name,
            "phone": phone,
            "numbers": numbers,
            "total": total,
            "language": language,
            "expires_at": cart["expires_at"],
        }, "ok"


def process_order_decision(order_id, decision):
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7410920261109)")
        order = conn.execute("SELECT * FROM orders WHERE order_id=%s", (order_id,)).fetchone()
        if not order or order["status"] != "awaiting_payment":
            return None, "processed"
        if from_iso(order["expires_at"]) <= utc_now():
            conn.execute("UPDATE orders SET status='expired' WHERE order_id=%s", (order_id,))
            conn.execute(
                "UPDATE numbers SET status='available',user_id=NULL,cart_token=NULL,expires_at=NULL "
                "WHERE status='reserved' AND cart_token=%s",
                (order["cart_token"],),
            )
            conn.execute("DELETE FROM carts WHERE user_id=%s", (order["user_id"],))
            return dict(order), "expired"
        numbers = json.loads(order["numbers_json"])
        for number in numbers:
            row = conn.execute("SELECT * FROM numbers WHERE number=%s", (number,)).fetchone()
            if not row or row["status"] != "reserved" or row["user_id"] != order["user_id"] or row["cart_token"] != order["cart_token"]:
                return None, "mismatch"
        if decision == "approve":
            for number in numbers:
                conn.execute(
                    "UPDATE numbers SET status='sold',user_id=NULL,cart_token=NULL,expires_at=NULL WHERE number=%s",
                    (number,),
                )
            conn.execute("UPDATE orders SET status='approved' WHERE order_id=%s", (order_id,))
        else:
            conn.execute(
                "UPDATE numbers SET status='available',user_id=NULL,cart_token=NULL,expires_at=NULL "
                "WHERE status='reserved' AND cart_token=%s",
                (order["cart_token"],),
            )
            conn.execute("UPDATE orders SET status='rejected' WHERE order_id=%s", (order_id,))
        conn.execute("DELETE FROM carts WHERE user_id=%s", (order["user_id"],))
        return dict(order), "ok"


def maybe_schedule_draw():
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7410920261109)")
        rows = conn.execute(
            "SELECT status,COUNT(*) AS count FROM numbers GROUP BY status"
        ).fetchall()
        counts = {"available": 0, "reserved": 0, "sold": 0}
        for row in rows:
            counts[row["status"]] = row["count"]
        if counts["sold"] != TOTAL_NUMBERS:
            return None
        values = conn.execute(
            "SELECT key,value FROM app_settings WHERE key IN ('draw_at','draw_completed')"
        ).fetchall()
        settings = {row["key"]: row["value"] for row in values}
        if settings.get("draw_completed") == "1":
            return from_iso(settings.get("draw_at"))
        existing = from_iso(settings.get("draw_at"))
        if existing:
            return existing
        draw_at = utc_now() + timedelta(minutes=DRAW_DELAY_MINUTES)
        conn.execute("UPDATE app_settings SET value=%s WHERE key='draw_at'", (to_iso(draw_at),))
        conn.execute("UPDATE app_settings SET value='0' WHERE key='warning_sent_5min'")
        conn.execute("UPDATE app_settings SET value='0' WHERE key='warning_live_sent'")
        return draw_at


def prepare_draw_if_due():
    with db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7410920261109)")
        settings = conn.execute(
            "SELECT key,value FROM app_settings WHERE key IN ('draw_at','draw_completed')"
        ).fetchall()
        values = {row["key"]: row["value"] for row in settings}
        draw_at = from_iso(values.get("draw_at"))
        if not draw_at or draw_at > utc_now() or values.get("draw_completed") == "1":
            return None
        sold = conn.execute(
            "SELECT number,user_id FROM numbers WHERE status='sold' ORDER BY number"
        ).fetchall()
        if len(sold) != TOTAL_NUMBERS:
            return None

        approved_orders = conn.execute(
            "SELECT user_id, customer_name, customer_phone, numbers_json "
            "FROM orders WHERE status='approved' ORDER BY created_at DESC"
        ).fetchall()

        number_owner_map = {}
        participant_ids = set()
        for ord_row in approved_orders:
            try:
                nums = json.loads(ord_row["numbers_json"])
            except Exception:
                nums = []
            for n in nums:
                if n not in number_owner_map:
                    number_owner_map[n] = {
                        "user_id": ord_row["user_id"],
                        "customer_name": ord_row["customer_name"],
                        "customer_phone": ord_row["customer_phone"],
                    }
                    participant_ids.add(ord_row["user_id"])

        for s in sold:
            if s["user_id"]:
                participant_ids.add(s["user_id"])
                if s["number"] not in number_owner_map:
                    number_owner_map[s["number"]] = {
                        "user_id": s["user_id"],
                        "customer_name": "Player",
                        "customer_phone": "N/A",
                    }
        if not participant_ids:
            participant_ids.add(HOST_TELEGRAM_ID)

        prize_count = min(len(PRIZES), len(sold))
        winners = random.SystemRandom().sample(list(sold), prize_count)
        now = to_iso(utc_now())
        current_round = get_current_round()

        conn.execute("UPDATE app_settings SET value='1' WHERE key='draw_completed'")
        wheel_initial = {
            "wheel_round_id": current_round,
            "wheel_status": "spinning",
            "wheel_active_rank": "1",
            "wheel_reveal_count": "0",
        }
        for key, value in wheel_initial.items():
            conn.execute(
                "INSERT INTO app_settings(key,value) VALUES(%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                (key, value),
            )

        result_rows = []
        for rank, (prize_name, amount) in enumerate(PRIZES[:prize_count], start=1):
            winner = winners[rank - 1]
            win_num = winner["number"]
            owner_info = number_owner_map.get(win_num) or {
                "user_id": winner["user_id"] or HOST_TELEGRAM_ID,
                "customer_name": "Player",
                "customer_phone": "N/A",
            }
            winner_uid = owner_info["user_id"]

            conn.execute(
                "INSERT INTO draw_results(prize_rank, round_id, prize_name, amount, number, winner_user_id, drawn_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(prize_rank) DO UPDATE SET "
                "round_id=EXCLUDED.round_id, prize_name=EXCLUDED.prize_name, amount=EXCLUDED.amount, number=EXCLUDED.number,"
                "winner_user_id=EXCLUDED.winner_user_id, drawn_at=EXCLUDED.drawn_at",
                (rank, current_round, prize_name, amount, win_num, winner_uid, now),
            )

            result_rows.append({
                "prize_rank": rank,
                "prize_name": prize_name,
                "amount": amount,
                "number": win_num,
                "winner_user_id": winner_uid,
                "customer_name": owner_info["customer_name"],
                "customer_phone": owner_info["customer_phone"],
            })

        users = [{"user_id": uid} for uid in participant_ids]
        return result_rows, users


def reset_after_draw():
    with db() as conn:
        conn.execute(
            "UPDATE numbers SET status='available',user_id=NULL,cart_token=NULL,expires_at=NULL"
        )
        conn.execute("DELETE FROM carts")
        conn.execute("UPDATE app_settings SET value=NULL WHERE key='draw_at'")
        conn.execute("UPDATE app_settings SET value='0' WHERE key='draw_completed'")
        conn.execute("UPDATE app_settings SET value='0' WHERE key='warning_sent_5min'")
        conn.execute("UPDATE app_settings SET value='0' WHERE key='warning_live_sent'")
        conn.execute("UPDATE app_settings SET value=NULL WHERE key='wheel_round_id'")
        conn.execute("UPDATE app_settings SET value='idle' WHERE key='wheel_status'")
        conn.execute("UPDATE app_settings SET value='0' WHERE key='wheel_active_rank'")
        conn.execute("UPDATE app_settings SET value='0' WHERE key='wheel_reveal_count'")
        conn.execute("UPDATE game_state SET round_number=round_number+1 WHERE id=1")
        row = conn.execute("SELECT round_number FROM game_state WHERE id=1").fetchone()
        return f"KLR-{row['round_number']:05d}"


async def send_live_draw_launch_notification(application):
    msg = (
        "🚨 **THE DRAW IS STARTING NOW!** 🎰\n\n"
        "The wheel is about to spin live. Open the Lucky Wheel right now to watch the three winners revealed!\n"
        "የዕጣ መንኮራኩር ሊጀምር ነው! አሸናፊዎችን በቀጥታ ለመከታተል አሁኑኑ ይክፈቱ!"
    )
    try:
        await application.bot.send_message(
            chat_id=SUPPORT_CHANNEL, text=msg, parse_mode="Markdown", reply_markup=wheel_launch_keyboard("en")
        )
    except Exception:
        pass

    all_users = await asyncio.to_thread(get_all_user_ids)
    for user_row in all_users:
        u_id = user_row["user_id"]
        lang = get_language(u_id)
        markup = wheel_launch_keyboard(u_id)
        user_msg = msg if lang == "en" else (
            "🚨 **ዕጣው አሁን እየጀመረ ነው!** 🎰\n\n"
            "መንኮራኩሩ በቀጥታ ሊሽከረከር ነው። ሦስቱ አሸናፊዎች አንድ በአንድ ሲገለጡ ለማየት የዕጣ መንኮራኩሩን አሁኑኑ ይክፈቱ!"
        )
        try:
            await application.bot.send_message(
                chat_id=u_id, text=user_msg, parse_mode="Markdown", reply_markup=markup
            )
            await asyncio.sleep(0.05)
        except Exception:
            pass


async def execute_draw_sequence(application):
    prepared = await asyncio.to_thread(prepare_draw_if_due)
    if not prepared:
        return
    result_rows, sold_users = prepared

    try:
        if WHEEL_WEBAPP_URL:
            for user_row in sold_users:
                u_id = user_row["user_id"]
                lang = get_language(u_id)
                launch_text = LANGUAGES[lang]["wheel_launch_msg"]
                launch_markup = wheel_launch_keyboard(u_id)
                try:
                    await application.bot.send_message(
                        chat_id=u_id,
                        text=launch_text,
                        parse_mode="Markdown",
                        reply_markup=launch_markup,
                    )
                    await asyncio.sleep(0.05)
                except Exception:
                    logger.exception("Could not send wheel launch button to participant")

            try:
                await application.bot.send_message(
                    chat_id=SUPPORT_CHANNEL,
                    text=LANGUAGES["en"]["wheel_launch_msg"],
                    parse_mode="Markdown",
                    reply_markup=wheel_launch_keyboard("en"),
                )
            except Exception:
                pass

        winner_user_ids = {row["winner_user_id"] for row in result_rows}
        sorted_results = sorted(result_rows, key=lambda x: x["prize_rank"])

        for row in sorted_results:
            rank = row["prize_rank"]
            rank_emoji = "🥇" if rank == 1 else ("🥈" if rank == 2 else "🥉")
            final_number = row["number"]

            await asyncio.to_thread(set_setting, "wheel_active_rank", str(rank))
            await asyncio.to_thread(set_setting, "wheel_status", "spinning")

            spin_msg = (
                f"🎰 **Spinning for {rank_emoji} {row['prize_name']}...** 🎡\n\n"
                f"Watch the live wheel spin in the Mini App!"
            )
            try:
                spin_message = await application.bot.send_message(
                    chat_id=SUPPORT_CHANNEL,
                    text=spin_msg,
                    parse_mode="Markdown",
                )
            except Exception:
                spin_message = None

            await asyncio.sleep(4.0)

            await asyncio.to_thread(set_setting, "wheel_reveal_count", str(rank))
            if rank == len(result_rows):
                await asyncio.to_thread(set_setting, "wheel_status", "complete")

            prize_text = (
                f"🏆 **OFFICIAL WINNER!**\n\n"
                f"{rank_emoji} **{row['prize_name']}**\n"
                f"Amount: **{row['amount']:,} ETB**\n"
                f"Winning Number: **{final_number}**\n"
                f"Winner: {row['customer_name']} ({row['customer_phone']})"
            )

            if spin_message:
                try:
                    await spin_message.edit_text(prize_text, parse_mode="Markdown")
                except Exception:
                    try:
                        await application.bot.send_message(chat_id=SUPPORT_CHANNEL, text=prize_text, parse_mode="Markdown")
                    except Exception:
                        pass
            else:
                try:
                    await application.bot.send_message(chat_id=SUPPORT_CHANNEL, text=prize_text, parse_mode="Markdown")
                except Exception:
                    pass

            for uid in winner_user_ids:
                if uid == row["winner_user_id"]:
                    lang = get_language(uid)
                    p_text = LANGUAGES[lang]["winner_private_msg"].format(
                        rank_emoji=rank_emoji,
                        prize_name=row["prize_name"],
                        amount=row["amount"],
                        final_number=final_number,
                        customer_name=row["customer_name"],
                        customer_phone=row["customer_phone"],
                    )
                    try:
                        await application.bot.send_message(
                            chat_id=uid,
                            text=p_text,
                            parse_mode="Markdown",
                        )
                    except Exception:
                        logger.exception("Could not notify winner privately")

            await asyncio.sleep(3)

        winner_details_lines_en = []
        for row in sorted_results:
            winner_details_lines_en.append(
                f"{row['prize_name']} ({row['amount']:,} ETB)\n"
                f"Number: {row['number']}\n"
                f"Winner Name: {row['customer_name']}\n"
                f"Phone: {row['customer_phone']}"
            )
        try:
            host_msg = LANGUAGES["en"]["host_payout_list"].format(winner_details="\n\n".join(winner_details_lines_en))
            await application.bot.send_message(chat_id=HOST_TELEGRAM_ID, text=host_msg)
        except Exception:
            logger.exception("Could not notify host")

        final_results_text_en = "\n\n".join([
            f"{row['prize_name']} — {row['amount']:,} ETB | Number {row['number']}"
            for row in sorted_results
        ])
        final_results_text_am = "\n\n".join([
            f"{row['prize_name']} — {row['amount']:,} ብር | ቁጥር {row['number']}"
            for row in sorted_results
        ])

        await asyncio.sleep(3)
        for user_row in sold_users:
            user_id = user_row["user_id"]
            if user_id in winner_user_ids:
                continue
            lang = get_language(user_id)
            res_text = (
                "🏆 KOSHKOSH LUCKY JAR DRAW RESULTS\n\n" + final_results_text_en
                if lang == "en" else
                "🏆 የኮሽኮሽ ሎተሪ የዕጣ ውጤቶች\n\n" + final_results_text_am
            )
            try:
                await application.bot.send_message(chat_id=user_id, text=res_text)
                await asyncio.sleep(0.05)
            except Exception:
                pass
    finally:
        new_round = await asyncio.to_thread(reset_after_draw)
        application.bot_data.pop("board_cache", None)

        try:
            support_new_round_text = (
                f"🔄 {new_round} ዙር ተጀመረ! የዕጣ ቁጥሮች አሁን ለአዲስ የዕጣ ዙር ጨዋታ ዝግጁ ናቸው። "
                f"ለ {new_round} የዕጣ ጨዋታ የዕድል ቁጥርዎትን አሁኑኑ ይምረጡ!"
            )
            await application.bot.send_message(
                chat_id=SUPPORT_CHANNEL,
                text=support_new_round_text,
            )
        except Exception:
            pass

        for user_row in sold_users:
            user_id = user_row["user_id"]
            try:
                priv_new_round_text = (
                    f"🔄 {new_round} ዙር ተጀመረ! የዕጣ ቁጥሮች አሁን ለአዲስ የዕጣ ዙር ጨዋታ ዝግጁ ናቸው። "
                    f"ለ {new_round} የዕጣ ጨዋታ የዕድል ቁጥርዎትን አሁኑኑ ይምረጡ!"
                )
                await application.bot.send_message(
                    chat_id=user_id,
                    text=priv_new_round_text,
                    reply_markup=main_menu_keyboard(user_id),
                )
                await asyncio.sleep(0.05)
            except Exception:
                pass


def get_sold_user_ids():
    with db() as conn:
        return conn.execute(
            "SELECT DISTINCT user_id FROM numbers WHERE status='sold' AND user_id IS NOT NULL"
        ).fetchall()


def get_all_user_ids():
    with db() as conn:
        return conn.execute(
            "SELECT user_id FROM users WHERE user_id IS NOT NULL"
        ).fetchall()


maintenance_lock = asyncio.Lock()


async def maintenance_job(context: ContextTypes.DEFAULT_TYPE):
    if maintenance_lock.locked():
        return
    async with maintenance_lock:
        try:
            await asyncio.to_thread(release_expired_reservations)
            context.bot_data.pop("board_cache", None)

            last_fomo = await asyncio.to_thread(setting, "last_fomo_sent")
            now = utc_now()

            try:
                last_fomo_time = from_iso(last_fomo) if last_fomo and last_fomo != "0" else None
            except Exception:
                last_fomo_time = None

            if not last_fomo_time or (now - last_fomo_time).total_seconds() >= 10800:
                await asyncio.to_thread(set_setting, "last_fomo_sent", to_iso(now))

                counts, _, sold_customers = await asyncio.to_thread(
                    get_cached_board_data, context, 1, TOTAL_NUMBERS
                )

                if sold_customers:
                    customer_lines = []
                    for c in sold_customers:
                        nums_str = ", ".join(map(str, c["numbers"]))
                        customer_lines.append(f"{c['customer_name']}    {c['customer_phone']}    {nums_str}")
                    raw_customer_list = "\n".join(customer_lines)
                else:
                    raw_customer_list = None

                channel_msg = LANGUAGES["en"]["board_text"].format(
                    available=counts["available"],
                    reserved=counts["reserved"],
                    sold=counts["sold"],
                    customer_list=raw_customer_list or LANGUAGES["en"]["no_sold_customers"],
                ) + f"\n\n👉 {BOT_USERNAME}"

                try:
                    await context.application.bot.send_message(
                        chat_id=SUPPORT_CHANNEL,
                        text=channel_msg,
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass

                all_users = await asyncio.to_thread(get_all_user_ids)
                for user_row in all_users:
                    u_id = user_row["user_id"]
                    lang = await asyncio.to_thread(get_language, u_id)
                    lang_dict = LANGUAGES.get(lang, LANGUAGES["en"])
                    user_customer_list = raw_customer_list or lang_dict["no_sold_customers"]

                    user_msg = lang_dict["board_text"].format(
                        available=counts["available"],
                        reserved=counts["reserved"],
                        sold=counts["sold"],
                        customer_list=user_customer_list,
                    ) + f"\n\n👉 {BOT_USERNAME}"

                    try:
                        await context.application.bot.send_message(
                            chat_id=u_id,
                            text=user_msg,
                            parse_mode="Markdown",
                        )
                        await asyncio.sleep(0.05)
                    except Exception:
                        pass

            draw_at, draw_completed = await asyncio.gather(
                asyncio.to_thread(get_draw_time),
                asyncio.to_thread(setting, "draw_completed"),
            )

            if draw_at and draw_completed == "0":
                now = utc_now()
                seconds_remaining = (draw_at - now).total_seconds()
                minutes_remaining = seconds_remaining / 60.0

                if 0.0 < seconds_remaining <= 45.0:
                    flag_key = "warning_live_sent"
                    flag_val = await asyncio.to_thread(setting, flag_key)
                    if flag_val != "1":
                        await asyncio.to_thread(set_setting, flag_key, "1")
                        await send_live_draw_launch_notification(context.application)

                elif 4.0 < minutes_remaining <= 5.0:
                    flag_key = "warning_sent_5min"
                    flag_val = await asyncio.to_thread(setting, flag_key)
                    if flag_val != "1":
                        await asyncio.to_thread(set_setting, flag_key, "1")
                        round_id = await asyncio.to_thread(get_current_round)

                        try:
                            await context.application.bot.send_message(
                                chat_id=SUPPORT_CHANNEL,
                                text=f"⏰ Only 5 minutes left! The draw for round {round_id} will start in 5 minutes. Get ready! 🎰",
                            )
                        except Exception:
                            logger.exception("Could not send 5-minute warning to support channel")

                        all_users = await asyncio.to_thread(get_all_user_ids)
                        for user_row in all_users:
                            u_id = user_row["user_id"]
                            try:
                                lang_msg = await asyncio.to_thread(tr, u_id, "warning_5min", round_id=round_id)
                                await context.application.bot.send_message(chat_id=u_id, text=lang_msg)
                                await asyncio.sleep(0.05)
                            except Exception:
                                pass

                if seconds_remaining <= 0:
                    await execute_draw_sequence(context.application)

            await asyncio.to_thread(maybe_schedule_draw)
        except Exception:
            logger.exception("Scheduled maintenance failed")


async def broadcast_sold_out(application, draw_at_time):
    round_id = await asyncio.to_thread(get_current_round)
    draw_time_str = format_ethiopia_time(draw_at_time)

    msg_en = LANGUAGES["en"]["sold_out"].format(round_id=round_id, draw_time=draw_time_str)

    try:
        await application.bot.send_message(chat_id=SUPPORT_CHANNEL, text=msg_en)
    except Exception:
        pass

    all_users = await asyncio.to_thread(get_all_user_ids)
    for user_row in all_users:
        u_id = user_row["user_id"]
        lang = get_language(u_id)
        msg_text = LANGUAGES[lang]["sold_out"].format(round_id=round_id, draw_time=draw_time_str)
        try:
            await application.bot.send_message(chat_id=u_id, text=msg_text)
            await asyncio.sleep(0.05)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# GAME HISTORY FUNCTIONS
# ---------------------------------------------------------------------------
def fetch_user_history(user_id):
    with db() as conn:
        tickets = conn.execute(
            "SELECT order_id, numbers_json, total, created_at FROM orders WHERE user_id=%s AND status='approved' ORDER BY created_at DESC LIMIT 5",
            (user_id,),
        ).fetchall()
        wins = conn.execute(
            """
            SELECT dr.round_id, dr.prize_name, dr.amount, dr.number, dr.drawn_at,
                   (SELECT customer_name FROM orders WHERE user_id = dr.winner_user_id LIMIT 1) as customer_name,
                   (SELECT customer_phone FROM orders WHERE user_id = dr.winner_user_id LIMIT 1) as customer_phone
            FROM draw_results dr
            WHERE dr.winner_user_id=%s
            ORDER BY dr.drawn_at DESC LIMIT 5
            """,
            (user_id,),
        ).fetchall()
    return tickets, wins


def fetch_global_history():
    with db() as conn:
        return conn.execute(
            """
            SELECT dr.round_id, dr.prize_name, dr.amount, dr.number, dr.drawn_at,
                   (SELECT customer_name FROM orders WHERE user_id = dr.winner_user_id LIMIT 1) as customer_name,
                   (SELECT customer_phone FROM orders WHERE user_id = dr.winner_user_id LIMIT 1) as customer_phone
            FROM draw_results dr
            ORDER BY dr.drawn_at DESC LIMIT 10
            """
        ).fetchall()


async def show_history(message, user_id):
    try:
        tickets, wins = await asyncio.to_thread(fetch_user_history, user_id)
        global_wins = await asyncio.to_thread(fetch_global_history)

        text = tr(user_id, "history_title") + "\n\n"

        text += tr(user_id, "history_tickets") + "\n"
        if tickets:
            for t in tickets:
                numbers_list = ", ".join(map(str, json.loads(t["numbers_json"])))
                text += tr(user_id, "history_ticket_line", order_id=t["order_id"], numbers=numbers_list, total=t["total"], date=t["created_at"][:19]) + "\n"
        else:
            text += tr(user_id, "history_no_tickets") + "\n"

        text += "\n" + tr(user_id, "history_my_wins") + "\n"
        if wins:
            for w in wins:
                text += f"[{w['round_id']}] " + tr(user_id, "history_win_line", prize=w["prize_name"], amount=w["amount"], number=w["number"], date=w["drawn_at"][:19]) + "\n"
                text += tr(user_id, "history_winner_info", name=w.get("customer_name") or "N/A", phone=w.get("customer_phone") or "N/A") + "\n"
        else:
            text += tr(user_id, "history_no_wins") + "\n"

        text += "\n" + tr(user_id, "history_global_wins") + "\n"
        if global_wins:
            for w in global_wins:
                text += f"[{w['round_id']}] " + tr(user_id, "history_win_line", prize=w["prize_name"], amount=w["amount"], number=w["number"], date=w["drawn_at"][:19]) + "\n"
                text += tr(user_id, "history_winner_info", name=w.get("customer_name") or "N/A", phone=w.get("customer_phone") or "N/A") + "\n"
        else:
            text += tr(user_id, "history_no_global_wins") + "\n"

        if hasattr(message, "edit_text"):
            await message.edit_text(text, reply_markup=back_keyboard(user_id), parse_mode="Markdown")
        else:
            await message.reply_text(text, reply_markup=back_keyboard(user_id), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in show_history: {e}")
        await message.reply_text("⚠️ Error loading history. Please try again.")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await asyncio.to_thread(ensure_user, user_id)
    try:
        await show_history(update.effective_message, user_id)
    except Exception as e:
        logger.error(f"History command error: {e}")
        await update.message.reply_text("⚠️ Error loading history. Please try again.")


# ---------------------------------------------------------------------------
# TELEGRAM HANDLERS
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await asyncio.to_thread(ensure_user, user_id)
    context.user_data.pop("awaiting", None)
    context.user_data.pop("cart_cache", None)
    round_id = await asyncio.to_thread(get_current_round)
    await update.message.reply_text(
        tr(user_id, "welcome", round_id=round_id), reply_markup=main_menu_keyboard(user_id)
    )


async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await asyncio.to_thread(ensure_user, user_id)
    await update.message.reply_text(
        tr(user_id, "choose_language"), reply_markup=language_keyboard()
    )


async def board_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(ensure_user, update.effective_user.id)
    await show_board_message(update.effective_message, update.effective_user.id, context)


async def show_board_message(message, user_id, context=None):
    counts, _, sold_customers = await asyncio.to_thread(
        get_cached_board_data, context, 1, TOTAL_NUMBERS
    ) if context else (lambda: (get_number_counts(), {}, []))()
    if not context:
        counts = get_number_counts()
        with db() as conn:
            sc_rows = conn.execute("SELECT customer_name, customer_phone, numbers_json FROM orders WHERE status='approved' ORDER BY created_at DESC").fetchall()
        sold_customers = []
        seen_nums = set()
        for sc in sc_rows:
            try:
                nums = json.loads(sc["numbers_json"])
            except Exception:
                nums = []
            valid_nums = [n for n in nums if n not in seen_nums]
            if valid_nums:
                for n in valid_nums:
                    seen_nums.add(n)
                sold_customers.append({
                    "customer_name": sc["customer_name"],
                    "customer_phone": sc["customer_phone"],
                    "numbers": sorted(valid_nums),
                })
    if sold_customers:
        customer_lines = []
        for c in sold_customers:
            nums_str = ", ".join(map(str, c["numbers"]))
            customer_lines.append(f"{c['customer_name']}    {c['customer_phone']}    {nums_str}")
        customer_list_str = "\n".join(customer_lines)
    else:
        customer_list_str = tr(user_id, "no_sold_customers")
    await message.reply_text(
        tr(
            user_id, "board_text",
            available=counts["available"],
            reserved=counts["reserved"],
            sold=counts["sold"],
            customer_list=customer_list_str,
        ),
        reply_markup=back_keyboard(user_id),
    )


async def draw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(ensure_user, update.effective_user.id)
    await show_draw_info(update.effective_message, update.effective_user.id)


async def show_draw_info(message, user_id):
    draw_at = await asyncio.to_thread(get_draw_time)
    draw_completed = await asyncio.to_thread(setting, "draw_completed")
    if not draw_at:
        await message.reply_text(
            tr(user_id, "draw_not_ready"), reply_markup=back_keyboard(user_id)
        )
        return
    seconds = (draw_at - utc_now()).total_seconds()
    if seconds <= 0 and draw_completed != "1":
        await message.reply_text(
            tr(user_id, "draw_pending", draw_time=format_ethiopia_time(draw_at), remaining="0s"),
            reply_markup=back_keyboard(user_id),
        )
        return
    if draw_completed == "1":
        await send_draw_results(message, user_id)
        return
    await message.reply_text(
        tr(
            user_id, "draw_pending",
            draw_time=format_ethiopia_time(draw_at),
            remaining=remaining_text(seconds),
        ),
        reply_markup=back_keyboard(user_id),
    )


def fetch_draw_results():
    with db() as conn:
        return conn.execute("SELECT * FROM draw_results ORDER BY prize_rank").fetchall()


async def send_draw_results(message, user_id):
    rows = await asyncio.to_thread(fetch_draw_results)
    if not rows:
        await message.reply_text(tr(user_id, "draw_not_ready"))
        return
    lines = [
        f"{row['prize_name']} — {row['amount']:,} ETB | "
        f"Number {row['number']}"
        for row in rows
    ]
    await message.reply_text(
        tr(user_id, "draw_results", results="\n\n".join(lines)),
        reply_markup=back_keyboard(user_id),
    )


async def _callback_notice(query, text):
    try:
        if query.message:
            await query.message.reply_text(text)
    except TelegramError:
        logger.debug("Could not send callback notice: %s", text, exc_info=True)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    ack_text = None
    if data.startswith("approve:"):
        ack_text = "⏳ Processing approval…"
    elif data.startswith("reject:"):
        ack_text = "⏳ Processing rejection…"
    elif data.startswith("toggle:"):
        ack_text = "⏳ Updating selection…"
    elif data.startswith("page:"):
        ack_text = "⏳ Loading page…"

    try:
        await query.answer(ack_text)
    except TelegramError:
        logger.debug("Callback could not be acknowledged (possibly stale).", exc_info=True)

    user_id = query.from_user.id
    await asyncio.to_thread(ensure_user, user_id)
    if data == "noop":
        await _callback_notice(query, "This number is reserved or sold.")
        return
    if data == "menu":
        context.user_data.pop("awaiting", None)
        context.user_data.pop("cart_cache", None)
        round_id = await asyncio.to_thread(get_current_round)
        await query.edit_message_text(
            tr(user_id, "welcome", round_id=round_id), reply_markup=main_menu_keyboard(user_id)
        )
        return
    if data == "play":
        await show_number_grid(query.message, user_id, context, page=1)
        return
    if data.startswith("page:"):
        try:
            page = int(data.split(":", 1)[1])
        except (TypeError, ValueError):
            page = 1
        await show_number_grid(query.message, user_id, context, page=page)
        return
    if data == "board":
        counts, _, sold_customers = await asyncio.to_thread(get_cached_board_data, context, 1, TOTAL_NUMBERS)
        if sold_customers:
            customer_lines = []
            for c in sold_customers:
                nums_str = ", ".join(map(str, c["numbers"]))
                customer_lines.append(f"{c['customer_name']}    {c['customer_phone']}    {nums_str}")
            customer_list_str = "\n".join(customer_lines)
        else:
            customer_list_str = tr(user_id, "no_sold_customers")
        await query.edit_message_text(
            tr(
                user_id, "board_text",
                available=counts["available"],
                reserved=counts["reserved"],
                sold=counts["sold"],
                customer_list=customer_list_str,
            ),
            reply_markup=back_keyboard(user_id),
        )
        return
    if data == "rules":
        await query.edit_message_text(
            tr(user_id, "rules_text", price=PRICE_PER_NUMBER, minutes=TRANSACTION_MINUTES, delay=DRAW_DELAY_MINUTES),
            reply_markup=back_keyboard(user_id),
        )
        return
    if data == "language":
        await query.edit_message_text(
            tr(user_id, "choose_language"), reply_markup=language_keyboard()
        )
        return
    if data.startswith("lang:"):
        language = data.split(":", 1)[1]
        if language not in ("en", "am"):
            await _callback_notice(query, "Invalid language.")
            return
        await asyncio.to_thread(set_language, user_id, language)
        LANGUAGE_CACHE[user_id] = (language, time.monotonic())
        await query.edit_message_text(
            tr(user_id, "language_changed"), reply_markup=back_keyboard(user_id)
        )
        return
    if data == "history":
        try:
            await show_history(query.message, user_id)
        except Exception as e:
            logger.error(f"History error: {e}")
            await query.message.reply_text("⚠️ Error loading history. Please try again.")
        return
    if data == "support":
        await query.edit_message_text(
            tr(user_id, "support_info"), reply_markup=back_keyboard(user_id)
        )
        return
    if data == "draw_info":
        draw_at, draw_completed = await asyncio.gather(
            asyncio.to_thread(get_draw_time),
            asyncio.to_thread(setting, "draw_completed"),
        )
        if not draw_at:
            text = tr(user_id, "draw_not_ready")
        elif draw_completed == "1":
            await query.edit_message_text(
                tr(user_id, "draw_results", results="Results are available. Use /draw."),
                reply_markup=back_keyboard(user_id),
            )
            return
        else:
            text = tr(
                user_id, "draw_pending",
                draw_time=format_ethiopia_time(draw_at),
                remaining=remaining_text((draw_at - utc_now()).total_seconds()),
            )
        await query.edit_message_text(text, reply_markup=back_keyboard(user_id))
        return
    if data.startswith("toggle:"):
        try:
            parts = data.split(":")
            number = int(parts[1])
            page = int(parts[2]) if len(parts) > 2 else 1
        except (TypeError, ValueError):
            await _callback_notice(query, "Invalid number.")
            return
        if number < 1 or number > TOTAL_NUMBERS:
            await _callback_notice(query, "Invalid number.")
            return
        result, numbers, counts, updated_cart = await asyncio.to_thread(
            toggle_number_transaction, user_id, number
        )
        context.user_data["cart_cache"] = updated_cart
        context.bot_data.pop("board_cache", None)
        if result == "locked":
            await _callback_notice(query, tr(user_id, "locked"))
            return
        if result == "taken":
            await _callback_notice(query, tr(user_id, "number_taken", number=number))
            return
        if result == "expired":
            await _callback_notice(query, tr(user_id, "expired"))
            return
        if result not in ("added", "removed"):
            await _callback_notice(query, "Unable to update selection. Please try again.")
            return
        await show_number_grid(query.message, user_id, context, page=page)
        return
    if data == "cancel":
        await asyncio.to_thread(cancel_user_cart, user_id)
        context.bot_data.pop("board_cache", None)
        context.user_data.pop("awaiting", None)
        context.user_data.pop("cart_cache", None)
        await query.edit_message_text(
            tr(user_id, "cancelled"), reply_markup=main_menu_keyboard(user_id)
        )
        return
    if data == "checkout":
        result = await asyncio.to_thread(begin_checkout, user_id)
        if result == "missing":
            await _callback_notice(query, tr(user_id, "no_selection"))
            return
        if result == "expired":
            await asyncio.to_thread(release_expired_reservations)
            await _callback_notice(query, tr(user_id, "expired"))
            return
        if result != "ok":
            await _callback_notice(query, tr(user_id, "locked"))
            return
        context.user_data["awaiting"] = "name"
        await query.message.reply_text(tr(user_id, "name_prompt"))
        return
    if data.startswith("approve:"):
        await approve_payment_callback(update, context)
        return
    if data.startswith("reject:"):
        await reject_payment_callback(update, context)
        return


async def handle_customer_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await asyncio.to_thread(ensure_user, user_id)
    text = (update.message.text or "").strip()
    awaiting = context.user_data.get("awaiting")
    if awaiting == "name":
        if len(text) < 2 or len(text) > 100:
            await update.message.reply_text(tr(user_id, "invalid_name"))
            return
        result = await asyncio.to_thread(save_customer_name, user_id, text)
        if result != "ok":
            context.user_data.pop("awaiting", None)
            context.user_data.pop("cart_cache", None)
            await update.message.reply_text(tr(user_id, "expired"))
            return
        context.user_data["awaiting"] = "phone"
        await update.message.reply_text(tr(user_id, "phone_prompt"))
        return
    if awaiting == "phone":
        phone = "".join(ch for ch in text if ch.isdigit() or ch == "+")
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) < 9 or len(digits) > 15:
            await update.message.reply_text(tr(user_id, "invalid_phone"))
            return
        order, status = await asyncio.to_thread(create_order, user_id, phone)
        context.user_data.pop("awaiting", None)
        context.user_data.pop("cart_cache", None)
        if status != "ok":
            await update.message.reply_text(
                tr(user_id, "expired") if status == "expired" else tr(user_id, "error_msg")
            )
            return
        expires = from_iso(order["expires_at"])
        round_id = await asyncio.to_thread(get_current_round)
        invoice = tr(
            user_id, "invoice",
            round_id=round_id,
            order_id=order["order_id"],
            name=order["name"],
            phone=order["phone"],
            numbers=", ".join(map(str, sorted(order["numbers"]))),
            total=order["total"],
            cbe=CBE_ACCOUNT,
            telebirr=TELEBIRR_NUMBER,
            cbebirr=CBEBIRR_NUMBER,
            expires=format_ethiopia_time(expires),
        )
        await update.message.reply_text(invoice, reply_markup=back_keyboard(user_id))
        return
    round_id = await asyncio.to_thread(get_current_round)
    await update.message.reply_text(
        tr(user_id, "main_menu", round_id=round_id), reply_markup=main_menu_keyboard(user_id)
    )


async def handle_payment_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await asyncio.to_thread(ensure_user, user_id)
    order = await asyncio.to_thread(get_active_order, user_id)
    if not order:
        await update.message.reply_text(tr(user_id, "send_screenshot"))
        return
    if from_iso(order["expires_at"]) <= utc_now():
        await asyncio.to_thread(release_expired_reservations)
        await update.message.reply_text(tr(user_id, "expired"))
        return
    caption = (
        f"📸 PAYMENT SCREENSHOT SUBMITTED\n"
        f"Order: {order['order_id']}\n"
        f"Customer: {order['customer_name']}\n"
        f"Phone: {order['customer_phone']}\n"
        f"User ID: {user_id}\n"
        f"Numbers: {', '.join(map(str, json.loads(order['numbers_json'])))}\n"
        f"Amount: {order['total']} Birr"
    )
    try:
        photo = update.message.photo[-1]
        await context.bot.send_photo(
            chat_id=SUPPORT_CHANNEL,
            photo=photo.file_id,
            caption=caption,
            reply_markup=approval_keyboard(order["order_id"], order["language"]),
        )
        await update.message.reply_text(tr(user_id, "screenshot_received"))
    except TelegramError:
        logger.exception("Failed to forward payment screenshot")
        await update.message.reply_text(tr(user_id, "error_msg"))
        try:
            await context.bot.send_message(
                chat_id=HOST_TELEGRAM_ID,
                text=(
                    f"⚠️ Failed to forward payment screenshot to the support channel.\n\n"
                    f"User ID: {user_id}\n"
                    f"Order: {order['order_id']}\n"
                    f"Name: {order['customer_name']}\n"
                    f"Phone: {order['customer_phone']}\n"
                    f"Numbers: {', '.join(map(str, json.loads(order['numbers_json'])))}\n"
                    f"Amount: {order['total']} Birr\n\n"
                    f"Please contact the user manually to complete verification."
                ),
            )
        except Exception:
            logger.exception("Also failed to notify host about the screenshot failure")


async def _set_support_decision_state(query, status_text):
    try:
        message = query.message
        if message and message.caption is not None:
            original = message.caption.split("\n\n⏳ Processing approval…", 1)[0]
            original = original.split("\n\n⏳ Processing rejection…", 1)[0]
            await query.edit_message_caption(
                caption=(original + "\n\n" + status_text)[:1024],
                reply_markup=None,
            )
        elif message and message.text is not None:
            original = message.text
            await query.edit_message_text(
                text=(original + "\n\n" + status_text)[:4096],
                reply_markup=None,
            )
        else:
            await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        logger.warning("Could not update support decision message state.", exc_info=True)


async def approve_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if query.from_user.id != HOST_TELEGRAM_ID:
        await _set_support_decision_state(query, "⚠️ Unauthorized approval attempt.")
        return
    order_id = query.data.split(":", 1)[1]
    await _set_support_decision_state(query, "⏳ Processing approval…")
    try:
        order, status = await asyncio.to_thread(process_order_decision, order_id, "approve")
    except Exception:
        logger.exception("Payment approval database operation failed for order %s", order_id)
        await _set_support_decision_state(query, "❌ Approval failed due to a processing error. Please check the order before retrying.")
        return
    if status == "expired":
        await _set_support_decision_state(query, "⌛ This order has expired; payment was not approved.")
        return
    if status != "ok" or not order:
        await _set_support_decision_state(query, "⚠️ Order not found, already processed, or reservation mismatch.")
        return
    await _set_support_decision_state(query, "✅ Payment approved.")
    try:
        await query.message.reply_text("✅ Payment approved for order " + order_id)
    except TelegramError:
        logger.debug("Could not post approval confirmation in support channel.", exc_info=True)
    LANGUAGE_CACHE[order["user_id"]] = (order["language"], time.monotonic())
    numbers = ", ".join(map(str, json.loads(order["numbers_json"])))
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=LANGUAGES[order["language"]]["payment_confirmed"].format(numbers=numbers),
            reply_markup=main_menu_keyboard(order["user_id"]),
        )
    except TelegramError:
        logger.exception("Could not notify customer of approval")
    context.application.bot_data.pop("board_cache", None)
    draw_at = await asyncio.to_thread(maybe_schedule_draw)
    if draw_at:
        counts = await asyncio.to_thread(get_number_counts)
        if counts["sold"] == TOTAL_NUMBERS:
            await broadcast_sold_out(context.application, draw_at)


async def reject_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    if query.from_user.id != HOST_TELEGRAM_ID:
        await _set_support_decision_state(query, "⚠️ Unauthorized rejection attempt.")
        return
    order_id = query.data.split(":", 1)[1]
    await _set_support_decision_state(query, "⏳ Processing rejection…")
    try:
        order, status = await asyncio.to_thread(process_order_decision, order_id, "reject")
    except Exception:
        logger.exception("Payment rejection database operation failed for order %s", order_id)
        await _set_support_decision_state(query, "❌ Rejection failed due to a processing error. Please check the order.")
        return
    if status != "ok" or not order:
        await _set_support_decision_state(query, "⚠️ Order not found, already processed, or expired.")
        return
    await _set_support_decision_state(query, "❌ Payment rejected.")
    try:
        await query.message.reply_text("❌ Payment rejected for order " + order_id)
    except TelegramError:
        logger.debug("Could not post rejection confirmation in support channel.", exc_info=True)
    context.application.bot_data.pop("board_cache", None)
    LANGUAGE_CACHE[order["user_id"]] = (order["language"], time.monotonic())
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=LANGUAGES[order["language"]]["payment_rejected"],
            reply_markup=main_menu_keyboard(order["user_id"]),
        )
    except TelegramError:
        logger.exception("Could not notify customer of rejection")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await asyncio.to_thread(cancel_user_cart, user_id)
    context.bot_data.pop("board_cache", None)
    context.user_data.pop("awaiting", None)
    context.user_data.pop("cart_cache", None)
    await update.message.reply_text(
        tr(user_id, "cancelled"), reply_markup=main_menu_keyboard(user_id)
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != HOST_TELEGRAM_ID:
        await update.message.reply_text(tr(update.effective_user.id, "host_only"))
        return
    await asyncio.to_thread(release_expired_reservations)
    counts, draw_at, round_id, draw_completed = await asyncio.gather(
        asyncio.to_thread(get_number_counts),
        asyncio.to_thread(get_draw_time),
        asyncio.to_thread(get_current_round),
        asyncio.to_thread(setting, "draw_completed"),
    )
    text = (
        f"KOSHKOSH ADMIN STATUS ({round_id})\n\n"
        f"Available: {counts['available']}\n"
        f"Reserved: {counts['reserved']}\n"
        f"Sold: {counts['sold']}\n"
        f"Draw scheduled: {format_ethiopia_time(draw_at) if draw_at else 'No'}\n"
        f"Draw completed: {draw_completed == '1'}"
    )
    await update.message.reply_text(text)


async def post_init(application: Application):
    init_db_pool()
    await asyncio.to_thread(init_db)
    await asyncio.to_thread(release_expired_reservations)
    application.job_queue.run_repeating(
        maintenance_job, interval=15, first=5, name="koshkosh-maintenance"
    )
    logger.info("KoshKosh database initialized with connection pool; maintenance scheduled.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(
        "Unhandled exception while processing update",
        exc_info=(type(context.error), context.error, context.error.__traceback__)
        if context.error else None,
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    if TOTAL_NUMBERS < 3:
        raise RuntimeError("TOTAL_NUMBERS must be at least 3 to award three prizes.")
    if PRICE_PER_NUMBER <= 0:
        raise RuntimeError("PRICE_PER_NUMBER must be greater than zero.")
    if TRANSACTION_MINUTES < 1 or DRAW_DELAY_MINUTES < 1:
        raise RuntimeError("TRANSACTION_MINUTES and DRAW_DELAY_MINUTES must be positive.")
    if not SUPPORT_CHANNEL:
        raise RuntimeError("SUPPORT_CHANNEL must be configured.")
    if WHEEL_WEBAPP_URL and (
        not WHEEL_WEBAPP_URL.startswith("https://")
        or not WHEEL_WEBAPP_URL.endswith("/wheel")
    ):
        raise RuntimeError("WHEEL_WEBAPP_URL must be a public HTTPS URL ending in /wheel.")

    threading.Thread(target=run_flask, daemon=True).start()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("language", language_command))
    application.add_handler(CommandHandler("board", board_command))
    application.add_handler(CommandHandler("draw", draw_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO, handle_payment_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_customer_text))
    application.add_error_handler(error_handler)

    logger.info("KoshKosh Lucky Jar Bot is starting...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
