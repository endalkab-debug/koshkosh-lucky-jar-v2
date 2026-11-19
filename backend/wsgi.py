"""Gunicorn entry point for the Mini App API service."""
import os
from flask import request
from koshkosh_bot import flask_app as app
from miniapp_api import miniapp_api, MAX_RECEIPT_BYTES

app.config["MAX_CONTENT_LENGTH"] = MAX_RECEIPT_BYTES + 1024 * 1024
app.register_blueprint(miniapp_api)

_ALLOWED_ORIGINS = {
    item.strip() for item in os.getenv("MINIAPP_ALLOWED_ORIGINS", "").split(",")
    if item.strip()
}

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin and origin in _ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Telegram-Init-Data"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return response

@app.route("/api/v1/<path:path>", methods=["OPTIONS"])
def api_preflight(path):
    return "", 204
