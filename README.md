# KoshKosh Lucky Jar — Mini App migration starter

This is **migration slice 2**, based on the uploaded production Python source and existing `wheel.html`.
It preserves the existing game/order tables and reuses the original transaction functions. It adds one additive `miniapp_receipts` metadata table for private payment receipt storage. It is not yet a production cutover.

## Project layout

- `backend/koshkosh_bot.py` — original uploaded bot source, copied without business-logic edits.
- `backend/wheel.html.legacy` — original wheel page retained as a reference.
- `backend/templates/wheel.html` — the same uploaded wheel page in the Flask template location, preserving the existing `/wheel` route.
- `backend/miniapp_api.py` — authenticated REST API, checkout, private receipt uploads, signed admin previews and Telegram notifications.
- `backend/wsgi.py` — Gunicorn entry point for the API service.
- `frontend/` — React + TypeScript premium player interface and an initial admin approval view.

## API in this first slice

All routes except `/health` require a valid Telegram Mini App `initData` value in `Authorization: tma <initData>`.

- `GET /api/v1/bootstrap` — player, round, pricing, prize, count and cart summary.
- `GET /api/v1/board` — public number statuses and counts; does not expose player IDs.
- `GET /api/v1/cart` — current player's cart.
- `POST /api/v1/cart/numbers` — toggle a number using the existing PostgreSQL transaction function.
- `DELETE /api/v1/cart` — release the current player's cart.
- `GET /api/v1/history` — the player's approved tickets and win history.
- `POST /api/v1/checkout/start` — locks the existing cart into the original checkout state machine.
- `POST /api/v1/checkout/name` — saves the customer name through the original function.
- `POST /api/v1/checkout/phone` — creates the existing order and returns the invoice/payment accounts.
- `POST /api/v1/checkout/{order_id}/receipt` — validates and uploads a JPEG/PNG/WebP screenshot (maximum 8 MB) to private Supabase Storage, then notifies the support channel with the existing Telegram approval callback format.
- `GET /api/v1/admin/orders` — host-only pending order list with receipt availability.
- `GET /api/v1/admin/orders/{order_id}/receipt-url` — host-only five-minute signed URL for private receipt viewing.
- `POST /api/v1/admin/orders/{order_id}/decision` — host-only approval/rejection through the existing decision function and customer Telegram notification.

## Local setup

### 1. Backend

```bash
cd backend
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set the shared environment variables from `.env.example`, especially `BOT_TOKEN`, `HOST_TELEGRAM_ID`, and `DATABASE_URL`. On the API service only, also set `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_RECEIPTS_BUCKET`, and `MAX_RECEIPT_BYTES`. Never add the service-role key to a `VITE_` frontend variable.

#### Create the private receipt bucket

In Supabase Dashboard → Storage → New bucket:

- Bucket name: `payment-receipts` (or the exact value of `SUPABASE_RECEIPTS_BUCKET`).
- Public bucket: **OFF**.
- File size limit: 8 MB (or match `MAX_RECEIPT_BYTES`).
- Allowed MIME types: `image/jpeg`, `image/png`, `image/webp`.

Do not add public read policies. The backend uses the service-role key to upload and generate short-lived signed viewing URLs. Keep that key only in the Render API service environment. The API creates the additive `miniapp_receipts` metadata table on first bootstrap/upload; it does not alter the existing game tables.

Run the API:

```bash
gunicorn --bind 0.0.0.0:10000 wsgi:app
```

Run the existing Telegram bot separately:

```bash
python koshkosh_bot.py
```

Both services must point to the same Supabase PostgreSQL database. The bot's current Flask thread remains in the original source for backward compatibility; the new API service is a separate process.

### 2. Frontend

```bash
cd frontend
npm install
# Set VITE_API_BASE_URL before building
npm run dev
```

For production, build with `npm run build` and deploy `frontend/dist` as a static site. Configure the API service's `MINIAPP_ALLOWED_ORIGINS` to the exact deployed frontend origin.

## Important deployment note

Do not change the live bot's Web App URL to the new player app until the frontend and API are deployed over HTTPS and the test checklist below passes. The currently running bot and wheel should remain available during the parallel migration.

## Remaining migration slices

1. Add durable notification retry/outbox handling and an auditable admin decision log; verify dashboard decisions and Telegram callback decisions have identical customer, channel and draw notifications.
2. Integrate the current live wheel into the React Mini App and test draw-state synchronization.
3. Move the bot's current customer-facing menu to a single Mini App launch button while retaining Telegram commands, payment notifications and admin approval fallback.
4. Add automated integration tests for reservation races, checkout expiry, duplicate orders, receipt ownership, duplicate receipt uploads, approval/rejection, round reset and initData replay/expiry.
5. Complete staging verification before production cutover.

## Safety and operational notes

- Never put `BOT_TOKEN`, Supabase service-role keys, or database credentials in frontend variables.
- Telegram user IDs are derived only from server-verified `initData`; the frontend cannot choose its own identity.
- The current database schema is reused. This starter does not run destructive migrations or reset game data.
- Test against a separate Supabase project or a backed-up staging database first.
- Telegram receipt notifications use the bot token from the API service and post a photo with the same `approve:<order_id>` / `reject:<order_id>` callback data handled by the existing bot worker. Dashboard decisions notify the customer directly; full channel-message editing and notification retry/outbox handling remain a later hardening task.
- Receipt uploads are stored in private Supabase Storage. The admin endpoint returns five-minute signed URLs only after server-side host authorization.
- The backend stores the receipt metadata and storage path, not a public URL. The browser never receives the Supabase service-role key.
