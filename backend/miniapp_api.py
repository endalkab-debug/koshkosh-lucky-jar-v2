"""Telegram-authenticated REST API layered over the existing KoshKosh logic.

The API reuses the original PostgreSQL transaction functions and adds one
non-destructive miniapp_receipts table for private payment receipt metadata.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
import logging
import requests
from functools import wraps
from urllib.parse import parse_qsl
from telegram_init_data import validate, parse

from flask import Blueprint, current_app, jsonify, request

import koshkosh_bot as legacy[cite: 1, 2]

miniapp_api = Blueprint("miniapp_api", __name__, url_prefix="/api/v1")[cite: 2]


def _verify_init_data(init_data: str) -> dict:[cite: 2]
    token = legacy.BOT_TOKEN[cite: 1, 2]
    if not token:[cite: 2]
        raise RuntimeError("BOT_TOKEN is not configured on the API service")[cite: 2]
    if not init_data:[cite: 2]
        raise ValueError("Missing Telegram initData")[cite: 2]

    try:[cite: 2]
        # 1. Validate the signature (this raises an error if invalid)
        validate(init_data, token)[cite: 2]
        
        # 2. Parse the data to get the user info
        parsed_data = parse(init_data)[cite: 2]
    except Exception as exc:[cite: 2]
        raise ValueError(f"Telegram initData signature verification failed: {exc}")[cite: 2]

    # Check expiration manually (since we removed the library's default check)
    max_age = int(os.getenv("TELEGRAM_INITDATA_MAX_AGE", "86400"))[cite: 2]
    now = int(time.time())[cite: 2]
    auth_date = int(parsed_data.get("auth_date", 0))[cite: 2]
    
    if auth_date <= 0 or auth_date > now + 60 or now - auth_date > max_age:[cite: 2]
        raise ValueError("Telegram initData has expired")[cite: 2]

    user = parsed_data.get("user")[cite: 2]
    if not user:[cite: 2]
        raise ValueError("Telegram user information is missing")[cite: 2]
        
    user_id = int(user["id"])[cite: 2]
    return {"user": user, "user_id": user_id, "auth_date": auth_date}[cite: 2]


def _current_user() -> dict:[cite: 2]
    header = request.headers.get("Authorization", "")[cite: 2]
    if header.startswith("tma "):[cite: 2]
        raw = header[4:].strip()[cite: 2]
    else:[cite: 2]
        raw = request.headers.get("X-Telegram-Init-Data", "").strip()[cite: 2]
    return _verify_init_data(raw)[cite: 2]


def require_telegram_user(fn):[cite: 2]
    @wraps(fn)
    def wrapped(*args, **kwargs):[cite: 2]
        try:[cite: 2]
            request.telegram_user = _current_user()[cite: 2]
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:[cite: 2]
            return jsonify({"error": str(exc), "code": "telegram_auth_failed"}), 401[cite: 2]
        except RuntimeError as exc:[cite: 2]
            current_app.logger.exception("Telegram authentication is not configured")[cite: 2]
            return jsonify({"error": str(exc), "code": "server_configuration_error"}), 503[cite: 2]
        return fn(*args, **kwargs)[cite: 2]
    return wrapped[cite: 2]


def require_admin(fn):[cite: 2]
    @wraps(fn)
    @require_telegram_user
    def wrapped(*args, **kwargs):[cite: 2]
        user_id = request.telegram_user["user_id"][cite: 2]
        if user_id != legacy.HOST_TELEGRAM_ID:[cite: 1, 2]
            return jsonify({"error": "Administrator access required."}), 403[cite: 2]
        return fn(*args, **kwargs)[cite: 2]
    return wrapped[cite: 2]


logger = logging.getLogger("koshkosh-miniapp-api")[cite: 2]
MAX_RECEIPT_BYTES = int(os.getenv("MAX_RECEIPT_BYTES", str(8 * 1024 * 1024)))[cite: 2]


def _supabase_config():[cite: 2]
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")[cite: 2]
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()[cite: 2]
    bucket = os.getenv("SUPABASE_RECEIPTS_BUCKET", "payment-receipts").strip()[cite: 2]
    if not url or not key or not bucket:[cite: 2]
        raise RuntimeError("Supabase Storage is not configured on the API service.")[cite: 2]
    return url, key, bucket[cite: 2]


def _ensure_receipt_table():[cite: 2]
    # Additive-only schema extension. Does not modify or reset existing tables.
    with legacy.db() as conn:[cite: 1, 2]
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
        """)[cite: 2]
        conn.execute("ALTER TABLE miniapp_receipts ADD COLUMN IF NOT EXISTS notification_status TEXT NOT NULL DEFAULT 'pending'")[cite: 2]


def _receipt_file_info(file_storage):[cite: 2]
    if not file_storage or not file_storage.filename:[cite: 2]
        raise ValueError("Choose a payment screenshot to upload.")[cite: 2]
    raw = file_storage.stream.read(MAX_RECEIPT_BYTES + 1)[cite: 2]
    if not raw:[cite: 2]
        raise ValueError("The uploaded file is empty.")[cite: 2]
    if len(raw) > MAX_RECEIPT_BYTES:[cite: 2]
        raise ValueError(f"Receipt must be {MAX_RECEIPT_BYTES // (1024 * 1024)} MB or smaller.")[cite: 2]
    # Verify the actual file signature; do not trust the browser's MIME declaration.
    if raw.startswith(b"\xff\xd8\xff"):[cite: 2]
        content_type, extension = "image/jpeg", "jpg"[cite: 2]
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):[cite: 2]
        content_type, extension = "image/png", "png"[cite: 2]
    elif len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":[cite: 2]
        content_type, extension = "image/webp", "webp"[cite: 2]
    else:[cite: 2]
        raise ValueError("Only JPEG, PNG or WebP payment screenshots are accepted.")[cite: 2]
    return raw, content_type, extension[cite: 2]


def _storage_upload(path: str, raw: bytes, content_type: str):[cite: 2]
    url, key, bucket = _supabase_config()[cite: 2]
    endpoint = f"{url}/storage/v1/object/{bucket}/{path}"[cite: 2]
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
    )[cite: 2]
    if response.status_code not in (200, 201):[cite: 2]
        logger.error("Supabase Storage upload failed: %s %s", response.status_code, response.text[:500])[cite: 2]
        raise RuntimeError("Receipt storage is temporarily unavailable. Please try again.")[cite: 2]


def _storage_signed_url(path: str, expires_seconds: int = 300) -> str:[cite: 2]
    url, key, bucket = _supabase_config()[cite: 2]
    response = requests.post(
        f"{url}/storage/v1/object/sign/{bucket}/{path}",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"expiresIn": expires_seconds},
        timeout=15,
    )[cite: 2]
    if response.status_code not in (200, 201):[cite: 2]
        logger.error("Supabase signed URL request failed: %s %s", response.status_code, response.text[:500])[cite: 2]
        raise RuntimeError("Could not create a private receipt viewing link.")[cite: 2]
    payload = response.json()[cite: 2]
    signed = payload.get("signedURL") or payload.get("signedUrl")[cite: 2]
    if not signed:[cite: 2]
        raise RuntimeError("Supabase did not return a signed receipt URL.")[cite: 2]
    if signed.startswith("http://") or signed.startswith("https://"):[cite: 2]
        return signed[cite: 2]
    return f"{url}/storage/v1{signed if signed.startswith('/') else '/' + signed}"[cite: 2]


def _telegram_api(method: str, *, data=None, files=None):[cite: 2]
    token = legacy.BOT_TOKEN[cite: 1, 2]
    if not token:[cite: 2]
        raise RuntimeError("BOT_TOKEN is not configured")[cite: 2]
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        files=files,
        timeout=30,
    )[cite: 2]
    try:[cite: 2]
        payload = response.json()[cite: 2]
    except ValueError:[cite: 2]
        payload = {"ok": False, "description": response.text[:300]}[cite: 2]
    if not response.ok or not payload.get("ok"):[cite: 2]
        raise RuntimeError(f"Telegram {method} failed: {payload.get('description', response.status_code)}")[cite: 2]
    return payload.get("result", {})[cite: 2]


def _notify_receipt_to_channel(order: dict, raw: bytes, content_type: str):[cite: 2]
    numbers = json.loads(order["numbers_json"]) if isinstance(order.get("numbers_json"), str) else order["numbers"][cite: 2]
    caption = (
        "📸 PAYMENT RECEIPT SUBMITTED\n\n"
        f"Order: {order['order_id']}\nCustomer: {order['customer_name']}\n"
        f"Phone: {order['customer_phone']}\nUser ID: {order['user_id']}\n"
        f"Numbers: {', '.join(map(str, numbers))}\nAmount: {order['total']} ETB\n\n"
        "Verify the payment in the receiving account before approving."
    )[cite: 2]
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": f"approve:{order['order_id']}"},
        {"text": "❌ Reject", "callback_data": f"reject:{order['order_id']}"},
    ]]}[cite: 2]
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[content_type][cite: 2]
    _telegram_api(
        "sendPhoto",
        data={"chat_id": legacy.SUPPORT_CHANNEL, "caption": caption, "reply_markup": json.dumps(keyboard)},
        files={"photo": (f"receipt.{ext}", raw, content_type)},
    )[cite: 1, 2]


def _notify_customer_decision(order: dict, decision: str):[cite: 2]
    language = order.get("language", "en")[cite: 2]
    labels = legacy.LANGUAGES.get(language, legacy.LANGUAGES["en"])[cite: 1, 2]
    if decision == "approve":[cite: 2]
        numbers = json.loads(order["numbers_json"]) if isinstance(order.get("numbers_json"), str) else order["numbers"][cite: 2]
        message = labels["payment_confirmed"].format(numbers=", ".join(map(str, numbers)))[cite: 1, 2]
    else:[cite: 2]
        message = labels["payment_rejected"][cite: 1, 2]
    _telegram_api("sendMessage", data={"chat_id": order["user_id"], "text": message})[cite: 2]


def _cart_payload(user_id: int) -> dict | None:[cite: 2]
    cart = legacy.get_cart(user_id)[cite: 1, 2]
    if not cart:[cite: 2]
        return None[cite: 2]
    expires = cart.get("expires")[cite: 1, 2]
    remaining = max(0, int((expires - legacy.utc_now()).total_seconds())) if expires else 0[cite: 1, 2]
    return {
        "numbers": sorted(int(n) for n in cart.get("numbers", [])),
        "state": cart.get("state"),
        "expires_at": cart.get("expires_at"),
        "remaining_seconds": remaining,
        "customer_name": cart.get("customer_name"),
    }[cite: 1, 2]


def _receipt_table_exists(conn) -> bool:[cite: 2]
    row = conn.execute("SELECT to_regclass('public.miniapp_receipts') AS table_name").fetchone()[cite: 2]
    return bool(row and row["table_name"])[cite: 2]


@miniapp_api.get("/bootstrap")
@require_telegram_user
def bootstrap():[cite: 2]
    user_id = request.telegram_user["user_id"][cite: 2]
    language = legacy.ensure_user(user_id)[cite: 1, 2]
    counts = legacy.get_number_counts()[cite: 1, 2]
    draw_at = legacy.get_draw_time()[cite: 1, 2]
    _ensure_receipt_table()[cite: 2]
    pending_order = None[cite: 2]
    with legacy.db() as conn:[cite: 1, 2]
        order_row = conn.execute(
            "SELECT o.order_id,o.customer_name,o.customer_phone,o.numbers_json,o.total,o.expires_at, "
            "(r.order_id IS NOT NULL) AS receipt_submitted "
            "FROM orders o LEFT JOIN miniapp_receipts r ON r.order_id=o.order_id "
            "WHERE o.user_id=%s AND o.status='awaiting_payment' ORDER BY o.created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone() if _receipt_table_exists(conn) else None[cite: 2]
    if order_row:[cite: 2]
        pending_order = dict(order_row)[cite: 2]
        pending_order["numbers"] = json.loads(pending_order.pop("numbers_json"))[cite: 2]
        pending_order["round_id"] = legacy.get_current_round()[cite: 1, 2]
        pending_order["currency"] = "ETB"[cite: 2]
        pending_order["payment_accounts"] = {"cbe": legacy.CBE_ACCOUNT, "telebirr": legacy.TELEBIRR_NUMBER, "cbebirr": legacy.CBEBIRR_NUMBER}[cite: 1, 2]
        pending_order["expires_at_ethiopia"] = legacy.format_ethiopia_time(legacy.from_iso(pending_order["expires_at"]))[cite: 1, 2]
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
            "draw_at": legacy.to_iso(draw_at) if draw_at else None,
            "draw_completed": legacy.setting("draw_completed") == "1",
        },
        "cart": _cart_payload(user_id),
        "pending_order": pending_order,
    })[cite: 1, 2]


@miniapp_api.get("/board")
@require_telegram_user
def board():[cite: 2]
    # Never expose another player's Telegram ID in the public number board.
    with legacy.db() as conn:[cite: 1, 2]
        rows = conn.execute("SELECT number,status FROM numbers ORDER BY number").fetchall()[cite: 2]
    counts = legacy.get_number_counts()[cite: 1, 2]
    return jsonify({
        "counts": counts,
        "numbers": [{"number": int(r["number"]), "status": r["status"]} for r in rows],
    })[cite: 2]


@miniapp_api.get("/cart")
@require_telegram_user
def get_cart_route():[cite: 2]
    return jsonify({"cart": _cart_payload(request.telegram_user["user_id"])})[cite: 2]


@miniapp_api.post("/cart/numbers")
@require_telegram_user
def toggle_cart_number():[cite: 2]
    user_id = request.telegram_user["user_id"][cite: 2]
    payload = request.get_json(silent=True) or {}[cite: 2]
    try:[cite: 2]
        number = int(payload.get("number"))[cite: 2]
    except (TypeError, ValueError):[cite: 2]
        return jsonify({"error": "A valid number is required."}), 400[cite: 2]
    if number < 1 or number > legacy.TOTAL_NUMBERS:[cite: 1, 2]
        return jsonify({"error": "Number is outside this round's range."}), 400[cite: 2]

    result, _numbers, counts, _cart = legacy.toggle_number_transaction(user_id, number)[cite: 1, 2]
    if result == "locked":[cite: 2]
        return jsonify({"error": "Checkout is already in progress. Finish or cancel it first.", "code": result}), 409[cite: 2]
    if result == "taken":[cite: 2]
        return jsonify({"error": "That number is no longer available.", "code": result, "counts": counts}), 409[cite: 2]
    if result not in ("added", "removed"):[cite: 2]
        return jsonify({"error": "Unable to update the selection.", "code": result}), 400[cite: 2]
    return jsonify({"result": result, "counts": counts, "cart": _cart_payload(user_id)})[cite: 2]


@miniapp_api.delete("/cart")
@require_telegram_user
def cancel_cart():[cite: 2]
    user_id = request.telegram_user["user_id"][cite: 2]
    legacy.cancel_user_cart(user_id)[cite: 1, 2]
    return jsonify({"ok": True, "cart": None, "counts": legacy.get_number_counts()})[cite: 1, 2]


@miniapp_api.get("/history")
@require_telegram_user
def history():[cite: 2]
    user_id = request.telegram_user["user_id"][cite: 2]
    tickets, wins = legacy.fetch_user_history(user_id)[cite: 1, 2]
    def ticket_row(row):[cite: 2]
        item = dict(row)[cite: 2]
        item["numbers"] = json.loads(item.pop("numbers_json", "[]"))[cite: 2]
        return item[cite: 2]
    return jsonify({
        "tickets": [ticket_row(row) for row in tickets],
        "wins": [dict(row) for row in wins],
    })[cite: 2]


@miniapp_api.post("/checkout/start")
@require_telegram_user
def checkout_start():[cite: 2]
    user_id = request.telegram_user["user_id"][cite: 2]
    result = legacy.begin_checkout(user_id)[cite: 1, 2]
    if result != "ok":[cite: 2]
        return jsonify({"error": {
            "missing": "Select at least one available number first.",
            "expired": "Your reservation has expired. Please select your numbers again.",
            "locked": "You already have a checkout in progress.",
        }.get(result, "Checkout could not be started."), "code": result}), 409[cite: 2]
    return jsonify({"ok": True, "cart": _cart_payload(user_id)})[cite: 2]


@miniapp_api.post("/checkout/name")
@require_telegram_user
def checkout_name():[cite: 2]
    user_id = request.telegram_user["user_id"][cite: 2]
    payload = request.get_json(silent=True) or {}[cite: 2]
    name = str(payload.get("name", "")).strip()[cite: 2]
    if len(name) < 2 or len(name) > 100:[cite: 2]
        return jsonify({"error": "Enter a name between 2 and 100 characters."}), 400[cite: 2]
    result = legacy.save_customer_name(user_id, name)[cite: 1, 2]
    if result != "ok":[cite: 2]
        return jsonify({"error": "Your reservation expired or checkout is no longer active.", "code": result}), 409[cite: 2]
    return jsonify({"ok": True})[cite: 2]


@miniapp_api.post("/checkout/phone")
@require_telegram_user
def checkout_phone():[cite: 2]
    user_id = request.telegram_user["user_id"][cite: 2]
    payload = request.get_json(silent=True) or {}[cite: 2]
    phone = "".join(ch for ch in str(payload.get("phone", "")) if ch.isdigit() or ch == "+")[cite: 2]
    digits = "".join(ch for ch in phone if ch.isdigit())[cite: 2]
    if len(digits) < 9 or len(digits) > 15:[cite: 2]
        return jsonify({"error": "Enter a valid phone number."}), 400[cite: 2]
    order, result = legacy.create_order(user_id, phone)[cite: 1, 2]
    if result != "ok" or not order:[cite: 2]
        return jsonify({"error": "Could not create your invoice. Please refresh your selection.", "code": result}), 409[cite: 2]
    expires = legacy.from_iso(order["expires_at"])[cite: 1, 2]
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
    }[cite: 1, 2]
    return jsonify({"ok": True, "invoice": invoice}), 201[cite: 2]


@miniapp_api.post("/checkout/<order_id>/receipt")
@require_telegram_user
def upload_payment_receipt(order_id: str):[cite: 2]
    user_id = request.telegram_user["user_id"][cite: 2]
    if request.content_length and request.content_length > MAX_RECEIPT_BYTES + 1024 * 1024:[cite: 2]
        return jsonify({"error": "Receipt upload is too large."}), 413[cite: 2]
    try:[cite: 2]
        raw, content_type, extension = _receipt_file_info(request.files.get("file"))[cite: 2]
        _ensure_receipt_table()[cite: 2]
        with legacy.db() as conn:[cite: 1, 2]
            order_row = conn.execute(
                "SELECT * FROM orders WHERE order_id=%s AND user_id=%s", (order_id, user_id)
            ).fetchone()[cite: 2]
            if not order_row:[cite: 2]
                return jsonify({"error": "Order not found."}), 404[cite: 2]
            order = dict(order_row)[cite: 2]
            if order["status"] != "awaiting_payment":[cite: 2]
                return jsonify({"error": "This order is no longer awaiting payment."}), 409[cite: 2]
            if legacy.from_iso(order["expires_at"]) <= legacy.utc_now():[cite: 1, 2]
                return jsonify({"error": "The payment window for this order has expired."}), 410[cite: 2]
            exists = conn.execute("SELECT 1 FROM miniapp_receipts WHERE order_id=%s", (order_id,)).fetchone()[cite: 2]
            if exists:[cite: 2]
                return jsonify({"error": "A receipt has already been submitted for this order."}), 409[cite: 2]

        storage_path = f"{order_id}/{user_id}/{uuid.uuid4().hex}.{extension}"[cite: 2]
        _storage_upload(storage_path, raw, content_type)[cite: 2]
        try:[cite: 2]
            with legacy.db() as conn:[cite: 1, 2]
                # Re-check the order after the external storage upload to close the decision race.
                latest = conn.execute(
                    "SELECT status,expires_at FROM orders WHERE order_id=%s AND user_id=%s FOR UPDATE",
                    (order_id, user_id),
                ).fetchone()[cite: 2]
                if not latest or latest["status"] != "awaiting_payment" or legacy.from_iso(latest["expires_at"]) <= legacy.utc_now():[cite: 1, 2]
                    raise ValueError("The order changed or expired while the receipt was uploading.")[cite: 2]
                conn.execute(
                    "INSERT INTO miniapp_receipts(order_id,user_id,storage_path,content_type,file_size,uploaded_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s)",
                    (order_id, user_id, storage_path, content_type, len(raw), legacy.to_iso(legacy.utc_now())),
                )[cite: 1, 2]
        except Exception:[cite: 2]
            # Best-effort cleanup of the uploaded object if the database write fails.
            try:[cite: 2]
                url, key, bucket = _supabase_config()[cite: 2]
                requests.delete(f"{url}/storage/v1/object/{bucket}/{storage_path}",
                    headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=15)[cite: 2]
            except Exception:[cite: 2]
                logger.exception("Could not clean up orphaned receipt object %s", storage_path)[cite: 2]
            raise[cite: 2]

        try:[cite: 2]
            _notify_receipt_to_channel(order, raw, content_type)[cite: 2]
            notification_status = "sent"[cite: 2]
        except Exception:[cite: 2]
            logger.exception("Receipt saved but support-channel notification failed for order %s", order_id)[cite: 2]
            notification_status = "pending_retry"[cite: 2]
            try:[cite: 2]
                _telegram_api("sendMessage", data={
                    "chat_id": legacy.HOST_TELEGRAM_ID,
                    "text": (f"⚠️ A payment receipt was uploaded but could not be posted to {legacy.SUPPORT_CHANNEL}.\n"
                             f"Order: {order_id}\nCustomer: {order['customer_name']}\n"
                             f"Amount: {order['total']} ETB\nOpen the Mini App admin dashboard to view the private receipt.")
                })[cite: 1, 2]
            except Exception:[cite: 2]
                logger.exception("Could not send receipt notification fallback to host for order %s", order_id)[cite: 2]
        with legacy.db() as conn:[cite: 1, 2]
            conn.execute("UPDATE miniapp_receipts SET notification_status=%s WHERE order_id=%s", (notification_status, order_id))[cite: 2]
        return jsonify({"ok": True, "order_id": order_id, "receipt_status": "submitted",
                        "notification_status": notification_status}), 201[cite: 2]
    except ValueError as exc:[cite: 2]
        return jsonify({"error": str(exc)}), 400[cite: 2]
    except RuntimeError as exc:[cite: 2]
        return jsonify({"error": str(exc)}), 503[cite: 2]
    except requests.RequestException:[cite: 2]
        logger.exception("Storage or Telegram network request failed for order %s", order_id)[cite: 2]
        return jsonify({"error": "A network service is temporarily unavailable. Please try again."}), 503[cite: 2]
    except Exception:[cite: 2]
        logger.exception("Receipt upload failed for order %s", order_id)[cite: 2]
        return jsonify({"error": "Receipt upload failed. Please contact support if the problem continues."}), 500[cite: 2]


@miniapp_api.get("/admin/orders")
@require_admin
def admin_orders():[cite: 2]
    with legacy.db() as conn:[cite: 1, 2]
        conn.execute("""
            CREATE TABLE IF NOT EXISTS miniapp_receipts (
                order_id TEXT PRIMARY KEY REFERENCES orders(order_id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL, storage_path TEXT NOT NULL UNIQUE,
                content_type TEXT NOT NULL, file_size BIGINT NOT NULL, uploaded_at TEXT NOT NULL,
                notification_status TEXT NOT NULL DEFAULT 'pending'
            )
        """)[cite: 2]
        conn.execute("ALTER TABLE miniapp_receipts ADD COLUMN IF NOT EXISTS notification_status TEXT NOT NULL DEFAULT 'pending'")[cite: 2]
        rows = conn.execute(
            "SELECT o.order_id,o.user_id,o.customer_name,o.customer_phone,o.numbers_json,o.total,o.language,o.created_at,o.expires_at, "
            "(r.order_id IS NOT NULL) AS has_receipt "
            "FROM orders o LEFT JOIN miniapp_receipts r ON r.order_id=o.order_id "
            "WHERE o.status='awaiting_payment' ORDER BY o.created_at ASC"
        ).fetchall()[cite: 2]
    return jsonify({"orders": [
        {**{k: v for k, v in dict(row).items() if k != "numbers_json"},
         "numbers": json.loads(row["numbers_json"])}
        for row in rows
    ]})[cite: 2]


@miniapp_api.get("/admin/orders/<order_id>/receipt-url")
@require_admin
def admin_receipt_url(order_id: str):[cite: 2]
    try:[cite: 2]
        _ensure_receipt_table()[cite: 2]
        with legacy.db() as conn:[cite: 1, 2]
            row = conn.execute("SELECT storage_path FROM miniapp_receipts WHERE order_id=%s", (order_id,)).fetchone()[cite: 2]
        if not row:[cite: 2]
            return jsonify({"error": "No receipt has been submitted for this order."}), 404[cite: 2]
        return jsonify({"url": _storage_signed_url(row["storage_path"], 300), "expires_in": 300})[cite: 2]
    except RuntimeError as exc:[cite: 2]
        return jsonify({"error": str(exc)}), 503[cite: 2]
    except Exception:[cite: 2]
        logger.exception("Could not generate receipt URL for order %s", order_id)[cite: 2]
        return jsonify({"error": "Receipt preview is temporarily unavailable."}), 503[cite: 2]


@miniapp_api.post("/admin/orders/<order_id>/decision")
@require_admin
def admin_order_decision(order_id: str):[cite: 2]
    payload = request.get_json(silent=True) or {}[cite: 2]
    decision = payload.get("decision")[cite: 2]
    if decision not in ("approve", "reject"):[cite: 2]
        return jsonify({"error": "decision must be approve or reject"}), 400[cite: 2]
    order, result = legacy.process_order_decision(order_id, decision)[cite: 1, 2]
    if result != "ok" or not order:[cite: 2]
        return jsonify({"error": f"Order could not be processed: {result}", "code": result}), 409[cite: 2]
    try:[cite: 2]
        _notify_customer_decision(order, decision)[cite: 2]
        notification_status = "sent"[cite: 2]
    except Exception:[cite: 2]
        logger.exception("Order %s was %s but customer notification failed", order_id, decision)[cite: 2]
        notification_status = "failed"[cite: 2]
    draw_at = None[cite: 2]
    if decision == "approve":[cite: 2]
        try:[cite: 2]
            draw_at = legacy.maybe_schedule_draw()[cite: 1, 2]
        except Exception:[cite: 2]
            logger.exception("Could not schedule draw after dashboard approval")[cite: 2]
    return jsonify({"ok": True, "order_id": order_id, "decision": decision,
                    "notification_status": notification_status,
                    "draw_at": legacy.to_iso(draw_at) if draw_at else None})[cite: 1, 2]


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

    if from_user_id != legacy.HOST_TELEGRAM_ID:[cite: 1]
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
    order, status = legacy.process_order_decision(order_id, decision)[cite: 1]

    if status == "ok" and order:
        badge = "✅ Payment approved." if decision == "approve" else "❌ Payment rejected."
        alert_text = "Payment approved!" if decision == "approve" else "Payment rejected."
        try:
            _notify_customer_decision(order, decision)
        except Exception:
            logger.exception("Customer notification failed for order %s", order_id)
        if decision == "approve":
            try:
                legacy.maybe_schedule_draw()[cite: 1]
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
