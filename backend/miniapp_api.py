"""Telegram-authenticated REST API layered over the existing KoshKosh logic.

The API reuses the original PostgreSQL transaction functions and adds one
non-destructive miniapp_receipts table for private payment receipt metadata.
"""
from __future__ import annotations

import json
import os
import time
import uuid
import random
import logging
import requests
from functools import wraps
from telegram_init_data import validate, parse

from flask import Blueprint, current_app, jsonify, request

import koshkosh_bot as legacy

miniapp_api = Blueprint("miniapp_api", __name__, url_prefix="/api/v1")


def _verify_init_data(init_data: str) -> dict:
    token = legacy.BOT_TOKEN
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured on the API service")
    if not init_data:
        raise ValueError("Missing Telegram initData")

    try:
        validate(init_data, token)
        parsed_data = parse(init_data)
    except Exception as exc:
        raise ValueError(f"Telegram initData signature verification failed: {exc}")

    max_age = int(os.getenv("TELEGRAM_INITDATA_MAX_AGE", "86400"))
    now = int(time.time())
    auth_date = int(parsed_data.get("auth_date", 0))

    if auth_date <= 0 or auth_date > now + 60 or now - auth_date > max_age:
        raise ValueError("Telegram initData has expired")

    user = parsed_data.get("user")
    if not user:
        raise ValueError("Telegram user information is missing")

    user_id = int(user["id"])
    return {"user": user, "user_id": user_id, "auth_date": auth_date}


def _current_user() -> dict:
    header = request.headers.get("Authorization", "")
    if header.startswith("tma "):
        raw = header[4:].strip()
    else:
        raw = request.headers.get("X-Telegram-Init-Data", "").strip()
    return _verify_init_data(raw)


def require_telegram_user(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            request.telegram_user = _current_user()
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            return jsonify({"error": str(exc), "code": "telegram_auth_failed"}), 401
        except RuntimeError as exc:
            current_app.logger.exception("Telegram authentication is not configured")
            return jsonify({"error": str(exc), "code": "server_configuration_error"}), 503
        return fn(*args, **kwargs)
    return wrapped


def require_admin(fn):
    @wraps(fn)
    @require_telegram_user
    def wrapped(*args, **kwargs):
        user_id = request.telegram_user["user_id"]
        if user_id != legacy.HOST_TELEGRAM_ID:
            return jsonify({"error": "Administrator access required."}), 403
        return fn(*args, **kwargs)
    return wrapped


logger = logging.getLogger("koshkosh-miniapp-api")
MAX_RECEIPT_BYTES = int(os.getenv("MAX_RECEIPT_BYTES", str(8 * 1024 * 1024)))


def _supabase_config():
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    bucket = os.getenv("SUPABASE_RECEIPTS_BUCKET", "payment-receipts").strip()
    if not url or not key or not bucket:
        raise RuntimeError("Supabase Storage is not configured on the API service.")
    return url, key, bucket


def _ensure_receipt_table():
    with legacy.db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS miniapp_receipts (
                order_id TEXT PRIMARY KEY REFERENCES orders(order_id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                storage_path TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL,
                file_size BIGINT NOT NULL,
                uploaded_at TEXT NOT NULL,
                notification_status TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        conn.execute("ALTER TABLE miniapp_receipts ADD COLUMN IF NOT EXISTS notification_status TEXT NOT NULL DEFAULT 'pending'")


def _receipt_file_info(file_storage):
    if not file_storage or not file_storage.filename:
        raise ValueError("Choose a payment screenshot to upload.")
    raw = file_storage.stream.read(MAX_RECEIPT_BYTES + 1)
    if not raw:
        raise ValueError("The uploaded file is empty.")
    if len(raw) > MAX_RECEIPT_BYTES:
        raise ValueError(f"Receipt must be {MAX_RECEIPT_BYTES // (1024 * 1024)} MB or smaller.")
    if raw.startswith(b"\xff\xd8\xff"):
        content_type, extension = "image/jpeg", "jpg"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type, extension = "image/png", "png"
    elif len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        content_type, extension = "image/webp", "webp"
    else:
        raise ValueError("Only JPEG, PNG or WebP payment screenshots are accepted.")
    return raw, content_type, extension


def _storage_upload(path: str, raw: bytes, content_type: str):
    url, key, bucket = _supabase_config()
    endpoint = f"{url}/storage/v1/object/{bucket}/{path}"
    response = requests.post(
        endpoint,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": content_type,
            "x-upsert": "false",
        },
        data=raw,
        timeout=30,
    )
    if response.status_code not in (200, 201):
        logger.error("Supabase Storage upload failed: %s %s", response.status_code, response.text[:500])
        raise RuntimeError("Receipt storage is temporarily unavailable. Please try again.")


def _storage_signed_url(path: str, expires_seconds: int = 300) -> str:
    url, key, bucket = _supabase_config()
    response = requests.post(
        f"{url}/storage/v1/object/sign/{bucket}/{path}",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"expiresIn": expires_seconds},
        timeout=15,
    )
    if response.status_code not in (200, 201):
        logger.error("Supabase signed URL request failed: %s %s", response.status_code, response.text[:500])
        raise RuntimeError("Could not create a private receipt viewing link.")
    payload = response.json()
    signed = payload.get("signedURL") or payload.get("signedUrl")
    if not signed:
        raise RuntimeError("Supabase did not return a signed receipt URL.")
    if signed.startswith("http://") or signed.startswith("https://"):
        return signed
    return f"{url}/storage/v1{signed if signed.startswith('/') else '/' + signed}"


def _telegram_api(method: str, *, data=None, files=None):
    token = legacy.BOT_TOKEN
    if not token:
        raise RuntimeError("BOT_TOKEN is not configured")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        files=files,
        timeout=30,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"ok": False, "description": response.text[:300]}
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description', response.status_code)}")
    return payload.get("result", {})


def _notify_receipt_to_channel(order: dict, raw: bytes, content_type: str):
    numbers = json.loads(order["numbers_json"]) if isinstance(order.get("numbers_json"), str) else order["numbers"]
    caption = (
        "📸 PAYMENT RECEIPT SUBMITTED\n\n"
        f"Order: {order['order_id']}\nCustomer: {order['customer_name']}\n"
        f"Phone: {order['customer_phone']}\nUser ID: {order['user_id']}\n"
        f"Numbers: {', '.join(map(str, numbers))}\nAmount: {order['total']} ETB\n\n"
        "Verify the payment in the receiving account before approving."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve:{order['order_id']}"},
        {"text": "❌ Reject", "callback_data": f"reject:{order['order_id']}"},
    ]]}
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type]
    _telegram_api(
        "sendPhoto",
        data={"chat_id": legacy.SUPPORT_CHANNEL, "caption": caption, "reply_markup": json.dumps(keyboard)},
        files={"photo": (f"receipt.{ext}", raw, content_type)},
    )


def _notify_customer_decision(order: dict, decision: str):
    language = order.get("language", "en")
    labels = legacy.LANGUAGES.get(language, legacy.LANGUAGES["en"])
    if decision == "approve":
        numbers = json.loads(order["numbers_json"]) if isinstance(order.get("numbers_json"), str) else order["numbers"]
        message = labels["payment_confirmed"].format(numbers=", ".join(map(str, numbers)))
    else:
        message = labels["payment_rejected"]
    _telegram_api("sendMessage", data={"chat_id": order["user_id"], "text": message})


def _notify_sold_out_via_api(draw_at):
    round_id = legacy.get_current_round()
    draw_time_str = legacy.format_ethiopia_time(draw_at)
    msg_en = legacy.LANGUAGES["en"]["sold_out"].format(round_id=round_id, draw_time=draw_time_str)
    try:
        _telegram_api("sendMessage", data={"chat_id": legacy.SUPPORT_CHANNEL, "text": msg_en})
    except Exception:
        logger.exception("Could not send sold-out broadcast to support channel")

    for row in legacy.get_all_user_ids():
        uid = row["user_id"]
        lang = legacy.get_language(uid)
        msg_text = legacy.LANGUAGES.get(lang, legacy.LANGUAGES["en"])["sold_out"].format(
            round_id=round_id,
            draw_time=draw_time_str,
        )
        try:
            _telegram_api("sendMessage", data={"chat_id": uid, "text": msg_text})
        except Exception:
            pass


def _notify_draw_winners_via_api(round_id: str, result_rows: list[dict], participant_ids: set[int], new_round: str):
    sorted_results = sorted(result_rows, key=lambda x: x["prize_rank"])
    winner_user_ids = {row["winner_user_id"] for row in sorted_results}

    channel_lines = ["🏆 **KOSHKOSH LUCKY JAR DRAW RESULTS**", f"Round: **{round_id}**", ""]
    host_lines = []
    for row in sorted_results:
        rank = row["prize_rank"]
        rank_emoji = "🥇" if rank == 1 else ("🥈" if rank == 2 else "🥉")
        channel_lines.append(
            f"{rank_emoji} **{row['prize_name']}** — **{row['amount']:,} ETB**\n"
            f"Winning Number: **{row['number']}** | {row['customer_name']} ({row['customer_phone']})"
        )
        host_lines.append(
            f"{row['prize_name']} ({row['amount']:,} ETB)\n"
            f"Number: {row['number']}\n"
            f"Winner Name: {row['customer_name']}\n"
            f"Phone: {row['customer_phone']}"
        )

    try:
        _telegram_api("sendMessage", data={
            "chat_id": legacy.SUPPORT_CHANNEL,
            "text": "\n".join(channel_lines),
            "parse_mode": "Markdown",
        })
    except Exception:
        logger.exception("Could not post draw results to support channel")

    try:
        host_msg = legacy.LANGUAGES["en"]["host_payout_list"].format(winner_details="\n\n".join(host_lines))
        _telegram_api("sendMessage", data={"chat_id": legacy.HOST_TELEGRAM_ID, "text": host_msg})
    except Exception:
        logger.exception("Could not notify host of payout list")

    for row in sorted_results:
        rank = row["prize_rank"]
        rank_emoji = "🥇" if rank == 1 else ("🥈" if rank == 2 else "🥉")
        uid = row["winner_user_id"]
        lang = legacy.get_language(uid)
        labels = legacy.LANGUAGES.get(lang, legacy.LANGUAGES["en"])
        p_text = labels["winner_private_msg"].format(
            rank_emoji=rank_emoji,
            prize_name=row["prize_name"],
            amount=row["amount"],
            final_number=row["number"],
            customer_name=row["customer_name"],
            customer_phone=row["customer_phone"],
        )
        try:
            _telegram_api("sendMessage", data={"chat_id": uid, "text": p_text, "parse_mode": "Markdown"})
        except Exception:
            pass

    final_en = "\n\n".join([f"{r['prize_name']} — {r['amount']:,} ETB | Number {r['number']}" for r in sorted_results])
    final_am = "\n\n".join([f"{r['prize_name']} — {r['amount']:,} ብር | ቁጥር {r['number']}" for r in sorted_results])
    next_round_am = (
        f"🔄 {new_round} ዙር ተጀመረ! የዕጣ ቁጥሮች አሁን ለአዲስ የዕጣ ዙር ጨዋታ ዝግጁ ናቸው። "
        f"ለ {new_round} የዕጣ ጨዋታ የዕድል ቁጥርዎትን አሁኑኑ ይምረጡ!"
    )
    for uid in participant_ids:
        if uid in winner_user_ids:
            continue
        lang = legacy.get_language(uid)
        res_text = ("🏆 KOSHKOSH LUCKY JAR DRAW RESULTS\n\n" + final_en) if lang == "en" else ("🏆 የኮሽኮሽ ሎተሪ የዕጣ ውጤቶች\n\n" + final_am)
        try:
            _telegram_api("sendMessage", data={"chat_id": uid, "text": res_text})
            _telegram_api("sendMessage", data={"chat_id": uid, "text": next_round_am})
        except Exception:
            pass


def _execute_miniapp_draw(force_if_sold_out: bool = False):
    with legacy.db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7410920261109)")
        settings_rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN ('draw_at','draw_completed')"
        ).fetchall()
        values = {row["key"]: row["value"] for row in settings_rows}
        draw_at = legacy.from_iso(values.get("draw_at"))
        already_completed = values.get("draw_completed") == "1"

        sold = conn.execute(
            "SELECT number, user_id FROM numbers WHERE status='sold' ORDER BY number"
        ).fetchall()
        if len(sold) != legacy.TOTAL_NUMBERS:
            return {"status": "not_ready", "reason": "All numbers are not sold yet."}

        now = legacy.utc_now()
        if not draw_at:
            if not force_if_sold_out:
                return {"status": "not_scheduled", "reason": "Draw has not been scheduled yet."}
            draw_at = now
        elif draw_at > now and not force_if_sold_out:
            return {
                "status": "pending",
                "seconds_remaining": int((draw_at - now).total_seconds()),
                "draw_at": legacy.to_iso(draw_at),
            }

        if already_completed:
            latest = conn.execute(
                "SELECT prize_rank, round_id, prize_name, amount, number, drawn_at "
                "FROM draw_results ORDER BY prize_rank"
            ).fetchall()
            return {"status": "already_completed", "results": [dict(r) for r in latest]}

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
            participant_ids.add(legacy.HOST_TELEGRAM_ID)

        prize_count = min(len(legacy.PRIZES), len(sold))
        winners = random.SystemRandom().sample(list(sold), prize_count)
        drawn_iso = legacy.to_iso(now)
        current_round = legacy.get_current_round()

        result_rows = []
        for rank, (prize_name, amount) in enumerate(legacy.PRIZES[:prize_count], start=1):
            winner = winners[rank - 1]
            win_num = winner["number"]
            owner_info = number_owner_map.get(win_num) or {
                "user_id": winner["user_id"] or legacy.HOST_TELEGRAM_ID,
                "customer_name": "Player",
                "customer_phone": "N/A",
            }
            winner_uid = owner_info["user_id"]
            conn.execute(
                "INSERT INTO draw_results(prize_rank, round_id, prize_name, amount, number, winner_user_id, drawn_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(prize_rank) DO UPDATE SET "
                "round_id=EXCLUDED.round_id, prize_name=EXCLUDED.prize_name, amount=EXCLUDED.amount, "
                "number=EXCLUDED.number, winner_user_id=EXCLUDED.winner_user_id, drawn_at=EXCLUDED.drawn_at",
                (rank, current_round, prize_name, amount, win_num, winner_uid, drawn_iso),
            )
            result_rows.append({
                "prize_rank": rank,
                "round_id": current_round,
                "prize_name": prize_name,
                "amount": amount,
                "number": win_num,
                "winner_user_id": winner_uid,
                "customer_name": owner_info["customer_name"],
                "customer_phone": owner_info["customer_phone"],
                "drawn_at": drawn_iso,
            })

        conn.execute("UPDATE numbers SET status='available',user_id=NULL,cart_token=NULL,expires_at=NULL")
        conn.execute("DELETE FROM carts")
        conn.execute("UPDATE app_settings SET value=NULL WHERE key='draw_at'")
        conn.execute("UPDATE app_settings SET value='0' WHERE key='draw_completed'")
        conn.execute("UPDATE app_settings SET value='0' WHERE key='warning_sent_5min'")
        conn.execute("UPDATE app_settings SET value='0' WHERE key='warning_live_sent'")
        conn.execute("UPDATE app_settings SET value=%s WHERE key='wheel_round_id'", (current_round,))
        conn.execute("UPDATE app_settings SET value='complete' WHERE key='wheel_status'")
        conn.execute("UPDATE app_settings SET value=%s WHERE key='wheel_active_rank'", (str(prize_count),))
        conn.execute("UPDATE app_settings SET value=%s WHERE key='wheel_reveal_count'", (str(prize_count),))
        conn.execute("UPDATE game_state SET round_number=round_number+1 WHERE id=1")
        next_row = conn.execute("SELECT round_number FROM game_state WHERE id=1").fetchone()
        new_round = f"KLR-{next_row['round_number']:05d}"

    return {
        "status": "drawn",
        "round_id": current_round,
        "new_round_id": new_round,
        "results": result_rows,
        "participant_ids": participant_ids,
    }


def _cart_payload(user_id: int) -> dict | None:
    cart = legacy.get_cart(user_id)
    if not cart:
        return None
    expires = cart.get("expires")
    remaining = max(0, int((expires - legacy.utc_now()).total_seconds())) if expires else 0
    return {
        "numbers": sorted(int(n) for n in cart.get("numbers", [])),
        "state": cart.get("state"),
        "expires_at": cart.get("expires_at"),
        "remaining_seconds": remaining,
        "customer_name": cart.get("customer_name"),
    }


def _draw_payload() -> dict:
    draw_at = legacy.get_draw_time()
    draw_completed = legacy.setting("draw_completed") == "1"
    now = legacy.utc_now()
    seconds_remaining = max(0, int((draw_at - now).total_seconds())) if draw_at else None

    try:
        wheel = legacy.get_wheel_snapshot()
    except Exception:
        wheel = {
            "round_id": legacy.get_current_round(),
            "status": "idle",
            "active_rank": 0,
            "revealed_count": 0,
            "total_numbers": legacy.TOTAL_NUMBERS,
            "results": [],
        }

    with legacy.db() as conn:
        latest_results = conn.execute(
            "SELECT prize_rank, round_id, prize_name, amount, number, drawn_at "
            "FROM draw_results ORDER BY prize_rank"
        ).fetchall()

    return {
        "draw_at": legacy.to_iso(draw_at) if draw_at else None,
        "draw_at_ethiopia": legacy.format_ethiopia_time(draw_at) if draw_at else None,
        "draw_completed": draw_completed,
        "seconds_remaining": seconds_remaining,
        "wheel_url": legacy.WHEEL_WEBAPP_URL or None,
        "wheel": wheel,
        "latest_results": [dict(r) for r in latest_results],
    }


def _receipt_table_exists(conn) -> bool:
    row = conn.execute("SELECT to_regclass('public.miniapp_receipts') AS table_name").fetchone()
    return bool(row and row["table_name"])


@miniapp_api.get("/bootstrap")
@require_telegram_user
def bootstrap():
    user_id = request.telegram_user["user_id"]
    language = legacy.ensure_user(user_id)
    counts = legacy.get_number_counts()
    draw_info = _draw_payload()
    _ensure_receipt_table()
    pending_order = None
    with legacy.db() as conn:
        order_row = conn.execute(
            "SELECT o.order_id,o.customer_name,o.customer_phone,o.numbers_json,o.total,o.expires_at, "
            "(r.order_id IS NOT NULL) AS receipt_submitted "
            "FROM orders o LEFT JOIN miniapp_receipts r ON r.order_id=o.order_id "
            "WHERE o.user_id=%s AND o.status='awaiting_payment' ORDER BY o.created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone() if _receipt_table_exists(conn) else None
    if order_row:
        pending_order = dict(order_row)
        pending_order["numbers"] = json.loads(pending_order.pop("numbers_json"))
        pending_order["round_id"] = legacy.get_current_round()
        pending_order["currency"] = "ETB"
        pending_order["payment_accounts"] = {
            "cbe": legacy.CBE_ACCOUNT,
            "telebirr": legacy.TELEBIRR_NUMBER,
            "cbebirr": legacy.CBEBIRR_NUMBER,
        }
        pending_order["expires_at_ethiopia"] = legacy.format_ethiopia_time(legacy.from_iso(pending_order["expires_at"]))
    return jsonify({
        "user": {
            "id": user_id,
            "first_name": request.telegram_user["user"].get("first_name", "Player"),
            "username": request.telegram_user["user"].get("username"),
            "language": language,
            "is_admin": user_id == legacy.HOST_TELEGRAM_ID,
        },
        "game": {
            "round_id": legacy.get_current_round(),
            "total_numbers": legacy.TOTAL_NUMBERS,
            "price_per_number": legacy.PRICE_PER_NUMBER,
            "transaction_minutes": legacy.TRANSACTION_MINUTES,
            "draw_delay_minutes": legacy.DRAW_DELAY_MINUTES,
            "prizes": [{"rank": i + 1, "name": name, "amount": amount}
                       for i, (name, amount) in enumerate(legacy.PRIZES)],
            "counts": counts,
            "draw_at": draw_info["draw_at"],
            "draw_at_ethiopia": draw_info["draw_at_ethiopia"],
            "draw_completed": draw_info["draw_completed"],
            "seconds_remaining": draw_info["seconds_remaining"],
            "wheel_url": draw_info["wheel_url"],
        },
        "draw": draw_info,
        "cart": _cart_payload(user_id),
        "pending_order": pending_order,
    })


@miniapp_api.get("/draw/state")
@require_telegram_user
def draw_state():
    legacy.release_expired_reservations()
    counts = legacy.get_number_counts()
    draw_info = _draw_payload()
    return jsonify({
        "round_id": legacy.get_current_round(),
        "counts": counts,
        "draw": draw_info,
    })


@miniapp_api.post("/draw/run")
@require_telegram_user
def run_draw_now():
    user_id = request.telegram_user["user_id"]
    payload = request.get_json(silent=True) or {}
    force_now = bool(payload.get("force_now")) and (user_id == legacy.HOST_TELEGRAM_ID)

    legacy.release_expired_reservations()
    outcome = _execute_miniapp_draw(force_if_sold_out=force_now)

    if outcome["status"] == "drawn":
        try:
            _notify_draw_winners_via_api(
                outcome["round_id"],
                outcome["results"],
                outcome["participant_ids"],
                outcome["new_round_id"],
            )
        except Exception:
            logger.exception("Draw succeeded in DB but Telegram notifications partially failed")

    return jsonify({
        "outcome": outcome.get("status"),
        "reason": outcome.get("reason"),
        "round_id": legacy.get_current_round(),
        "counts": legacy.get_number_counts(),
        "draw": _draw_payload(),
        "results": outcome.get("results") or [],
    })


@miniapp_api.get("/board")
@require_telegram_user
def board():
    with legacy.db() as conn:
        rows = conn.execute("SELECT number,status FROM numbers ORDER BY number").fetchall()
    counts = legacy.get_number_counts()
    return jsonify({
        "counts": counts,
        "numbers": [{"number": int(r["number"]), "status": r["status"]} for r in rows],
    })


@miniapp_api.get("/cart")
@require_telegram_user
def get_cart_route():
    return jsonify({"cart": _cart_payload(request.telegram_user["user_id"])})


@miniapp_api.post("/cart/numbers")
@require_telegram_user
def toggle_cart_number():
    user_id = request.telegram_user["user_id"]
    payload = request.get_json(silent=True) or {}
    try:
        number = int(payload.get("number"))
    except (TypeError, ValueError):
        return jsonify({"error": "A valid number is required."}), 400
    if number < 1 or number > legacy.TOTAL_NUMBERS:
        return jsonify({"error": "Number is outside this round's range."}), 400

    result, _numbers, counts, _cart = legacy.toggle_number_transaction(user_id, number)
    if result == "locked":
        return jsonify({"error": "Checkout is already in progress. Finish or cancel it first.", "code": result}), 409
    if result == "taken":
        return jsonify({"error": "That number is no longer available.", "code": result, "counts": counts}), 409
    if result not in ("added", "removed"):
        return jsonify({"error": "Unable to update the selection.", "code": result}), 400
    return jsonify({"result": result, "counts": counts, "cart": _cart_payload(user_id)})


@miniapp_api.delete("/cart")
@require_telegram_user
def cancel_cart():
    user_id = request.telegram_user["user_id"]
    legacy.cancel_user_cart(user_id)
    return jsonify({"ok": True, "cart": None, "counts": legacy.get_number_counts()})


@miniapp_api.get("/history")
@require_telegram_user
def history():
    user_id = request.telegram_user["user_id"]
    tickets, wins = legacy.fetch_user_history(user_id)
    global_wins = legacy.fetch_global_history()

    def ticket_row(row):
        item = dict(row)
        item["numbers"] = json.loads(item.pop("numbers_json", "[]"))
        return item

    return jsonify({
        "tickets": [ticket_row(row) for row in tickets],
        "wins": [dict(row) for row in wins],
        "global_wins": [dict(row) for row in global_wins],
    })


@miniapp_api.post("/checkout/start")
@require_telegram_user
def checkout_start():
    user_id = request.telegram_user["user_id"]
    result = legacy.begin_checkout(user_id)
    if result != "ok":
        return jsonify({"error": {
            "missing": "Select at least one available number first.",
            "expired": "Your reservation has expired. Please select your numbers again.",
            "locked": "You already have a checkout in progress.",
        }.get(result, "Checkout could not be started."), "code": result}), 409
    return jsonify({"ok": True, "cart": _cart_payload(user_id)})


@miniapp_api.post("/checkout/name")
@require_telegram_user
def checkout_name():
    user_id = request.telegram_user["user_id"]
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    if len(name) < 2 or len(name) > 100:
        return jsonify({"error": "Enter a name between 2 and 100 characters."}), 400
    result = legacy.save_customer_name(user_id, name)
    if result != "ok":
        return jsonify({"error": "Your reservation expired or checkout is no longer active.", "code": result}), 409
    return jsonify({"ok": True})


@miniapp_api.post("/checkout/phone")
@require_telegram_user
def checkout_phone():
    user_id = request.telegram_user["user_id"]
    payload = request.get_json(silent=True) or {}
    phone = "".join(ch for ch in str(payload.get("phone", "")) if ch.isdigit() or ch == "+")
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 9 or len(digits) > 15:
        return jsonify({"error": "Enter a valid phone number."}), 400
    order, result = legacy.create_order(user_id, phone)
    if result != "ok" or not order:
        return jsonify({"error": "Could not create your invoice. Please refresh your selection.", "code": result}), 409
    expires = legacy.from_iso(order["expires_at"])
    invoice = {
        "order_id": order["order_id"],
        "round_id": legacy.get_current_round(),
        "customer_name": order["name"],
        "customer_phone": order["phone"],
        "numbers": sorted(order["numbers"]),
        "total": order["total"],
        "currency": "ETB",
        "payment_accounts": {
            "cbe": legacy.CBE_ACCOUNT,
            "telebirr": legacy.TELEBIRR_NUMBER,
            "cbebirr": legacy.CBEBIRR_NUMBER,
        },
        "expires_at": legacy.to_iso(expires),
        "expires_at_ethiopia": legacy.format_ethiopia_time(expires),
        "status": "awaiting_payment",
    }
    return jsonify({"ok": True, "invoice": invoice}), 201


@miniapp_api.post("/checkout/<order_id>/receipt")
@require_telegram_user
def upload_payment_receipt(order_id: str):
    user_id = request.telegram_user["user_id"]
    if request.content_length and request.content_length > MAX_RECEIPT_BYTES + 1024 * 1024:
        return jsonify({"error": "Receipt upload is too large."}), 413
    try:
        raw, content_type, extension = _receipt_file_info(request.files.get("file"))
        _ensure_receipt_table()
        with legacy.db() as conn:
            order_row = conn.execute(
                "SELECT * FROM orders WHERE order_id=%s AND user_id=%s", (order_id, user_id)
            ).fetchone()
            if not order_row:
                return jsonify({"error": "Order not found."}), 404
            order = dict(order_row)
            if order["status"] != "awaiting_payment":
                return jsonify({"error": "This order is no longer awaiting payment."}), 409
            if legacy.from_iso(order["expires_at"]) <= legacy.utc_now():
                return jsonify({"error": "The payment window for this order has expired."}), 410
            exists = conn.execute("SELECT 1 FROM miniapp_receipts WHERE order_id=%s", (order_id,)).fetchone()
            if exists:
                return jsonify({"error": "A receipt has already been submitted for this order."}), 409

        storage_path = f"{order_id}/{user_id}/{uuid.uuid4().hex}.{extension}"
        _storage_upload(storage_path, raw, content_type)
        try:
            with legacy.db() as conn:
                latest = conn.execute(
                    "SELECT status,expires_at FROM orders WHERE order_id=%s AND user_id=%s FOR UPDATE",
                    (order_id, user_id),
                ).fetchone()
                if not latest or latest["status"] != "awaiting_payment" or legacy.from_iso(latest["expires_at"]) <= legacy.utc_now():
                    raise ValueError("The order changed or expired while the receipt was uploading.")
                conn.execute(
                    "INSERT INTO miniapp_receipts(order_id,user_id,storage_path,content_type,file_size,uploaded_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (order_id, user_id, storage_path, content_type, len(raw), legacy.to_iso(legacy.utc_now())),
                )
        except Exception:
            try:
                url, key, bucket = _supabase_config()
                requests.delete(
                    f"{url}/storage/v1/object/{bucket}/{storage_path}",
                    headers={"apikey": key, "Authorization": f"Bearer {key}"},
                    timeout=15,
                )
            except Exception:
                logger.exception("Could not clean up orphaned receipt object %s", storage_path)
            raise

        try:
            _notify_receipt_to_channel(order, raw, content_type)
            notification_status = "sent"
        except Exception:
            logger.exception("Receipt saved but support-channel notification failed for order %s", order_id)
            notification_status = "pending_retry"
            try:
                _telegram_api("sendMessage", data={
                    "chat_id": legacy.HOST_TELEGRAM_ID,
                    "text": (
                        f"⚠️ A payment receipt was uploaded but could not be posted to {legacy.SUPPORT_CHANNEL}.\n"
                        f"Order: {order_id}\nCustomer: {order['customer_name']}\n"
                        f"Amount: {order['total']} ETB\nOpen the Mini App admin dashboard to view the private receipt."
                    ),
                })
            except Exception:
                logger.exception("Could not send receipt notification fallback to host for order %s", order_id)
        with legacy.db() as conn:
            conn.execute("UPDATE miniapp_receipts SET notification_status=%s WHERE order_id=%s", (notification_status, order_id))
        return jsonify({
            "ok": True,
            "order_id": order_id,
            "receipt_status": "submitted",
            "notification_status": notification_status,
        }), 201
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except requests.RequestException:
        logger.exception("Storage or Telegram network request failed for order %s", order_id)
        return jsonify({"error": "A network service is temporarily unavailable. Please try again."}), 503
    except Exception:
        logger.exception("Receipt upload failed for order %s", order_id)
        return jsonify({"error": "Receipt upload failed. Please contact support if the problem continues."}), 500


@miniapp_api.get("/admin/orders")
@require_admin
def admin_orders():
    with legacy.db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS miniapp_receipts (
                order_id TEXT PRIMARY KEY REFERENCES orders(order_id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL, storage_path TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL, file_size BIGINT NOT NULL, uploaded_at TEXT NOT NULL,
                notification_status TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        conn.execute("ALTER TABLE miniapp_receipts ADD COLUMN IF NOT EXISTS notification_status TEXT NOT NULL DEFAULT 'pending'")
        rows = conn.execute(
            "SELECT o.order_id,o.user_id,o.customer_name,o.customer_phone,o.numbers_json,o.total,o.language,o.created_at,o.expires_at, "
            "(r.order_id IS NOT NULL) AS has_receipt "
            "FROM orders o LEFT JOIN miniapp_receipts r ON r.order_id=o.order_id "
            "WHERE o.status='awaiting_payment' ORDER BY o.created_at ASC"
        ).fetchall()
    return jsonify({"orders": [
        {**{k: v for k, v in dict(row).items() if k != "numbers_json"},
         "numbers": json.loads(row["numbers_json"])}
        for row in rows
    ]})


@miniapp_api.get("/admin/orders/<order_id>/receipt-url")
@require_admin
def admin_receipt_url(order_id: str):
    try:
        _ensure_receipt_table()
        with legacy.db() as conn:
            row = conn.execute("SELECT storage_path FROM miniapp_receipts WHERE order_id=%s", (order_id,)).fetchone()
        if not row:
            return jsonify({"error": "No receipt has been submitted for this order."}), 404
        return jsonify({"url": _storage_signed_url(row["storage_path"], 300), "expires_in": 300})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception:
        logger.exception("Could not generate receipt URL for order %s", order_id)
        return jsonify({"error": "Receipt preview is temporarily unavailable."}), 503


@miniapp_api.post("/admin/orders/<order_id>/decision")
@require_admin
def admin_order_decision(order_id: str):
    payload = request.get_json(silent=True) or {}
    decision = payload.get("decision")
    if decision not in ("approve", "reject"):
        return jsonify({"error": "decision must be approve or reject"}), 400
    order, result = legacy.process_order_decision(order_id, decision)
    if result != "ok" or not order:
        return jsonify({"error": f"Order could not be processed: {result}", "code": result}), 409
    try:
        _notify_customer_decision(order, decision)
        notification_status = "sent"
    except Exception:
        logger.exception("Order %s was %s but customer notification failed", order_id, decision)
        notification_status = "failed"
    draw_at = None
    if decision == "approve":
        try:
            draw_at = legacy.maybe_schedule_draw()
            if draw_at and legacy.get_number_counts()["sold"] == legacy.TOTAL_NUMBERS:
                _notify_sold_out_via_api(draw_at)
        except Exception:
            logger.exception("Could not schedule draw after dashboard approval")
    return jsonify({
        "ok": True,
        "order_id": order_id,
        "decision": decision,
        "notification_status": notification_status,
        "draw_at": legacy.to_iso(draw_at) if draw_at else None,
    })


@miniapp_api.post("/telegram/webhook")
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    callback = update.get("callback_query")
    if not callback:
        return jsonify({"ok": True}), 200

    callback_id = callback.get("id")
    data = callback.get("data", "")
    from_user_id = (callback.get("from") or {}).get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    original_caption = message.get("caption") or ""

    if not (data.startswith("approve:") or data.startswith("reject:")):
        if callback_id:
            try:
                _telegram_api("answerCallbackQuery", data={"callback_query_id": callback_id})
            except Exception:
                pass
        return jsonify({"ok": True}), 200

    if from_user_id != legacy.HOST_TELEGRAM_ID:
        if callback_id:
            try:
                _telegram_api("answerCallbackQuery", data={
                    "callback_query_id": callback_id,
                    "text": "Only the authorized host can do this.",
                    "show_alert": True,
                })
            except Exception:
                pass
        return jsonify({"ok": True}), 200

    decision, order_id = data.split(":", 1)
    order, status = legacy.process_order_decision(order_id, decision)

    if status == "ok" and order:
        badge = "✅ Payment approved." if decision == "approve" else "❌ Payment rejected."
        alert_text = "Payment approved!" if decision == "approve" else "Payment rejected."
        try:
            _notify_customer_decision(order, decision)
        except Exception:
            logger.exception("Customer notification failed for order %s", order_id)
        if decision == "approve":
            try:
                draw_at = legacy.maybe_schedule_draw()
                if draw_at and legacy.get_number_counts()["sold"] == legacy.TOTAL_NUMBERS:
                    _notify_sold_out_via_api(draw_at)
            except Exception:
                logger.exception("Draw scheduling check failed after approval")
    elif status == "expired":
        badge = "⌛ Order expired."
        alert_text = "This order has already expired."
    else:
        badge = "⚠️ Already processed."
        alert_text = "Order not found or already processed."

    if chat_id and message_id:
        try:
            _telegram_api("editMessageCaption", data={
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": f"{original_caption}\n\n{badge}"[:1024],
                "reply_markup": json.dumps({"inline_keyboard": []}),
            })
        except Exception:
            logger.exception("Could not update support channel caption for order %s", order_id)

    if callback_id:
        try:
            _telegram_api("answerCallbackQuery", data={
                "callback_query_id": callback_id,
                "text": alert_text,
            })
        except Exception:
            pass

    return jsonify({"ok": True}), 200
