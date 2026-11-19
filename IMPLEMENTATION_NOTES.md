# Source-code audit and migration map

## Existing uploaded source

The uploaded `Pasted text(7).txt` contains 2,223 lines. It defines a Flask app and a python-telegram-bot polling process in one Python program. Its database helpers use `psycopg` / `psycopg_pool` and the existing tables are `users`, `numbers`, `carts`, `orders`, `app_settings`, `draw_results`, and `game_state`.

Key existing functions reused by the API:

- `ensure_user`, `get_language`, `get_current_round`, `get_number_counts`
- `get_cart`, `toggle_number_transaction`, `cancel_user_cart`
- `get_draw_time`, `fetch_user_history`
- `process_order_decision`

The existing number-toggle function uses a PostgreSQL advisory transaction lock and updates the cart and number reservation in the same transaction. The Mini App calls that function instead of reimplementing reservation logic in JavaScript.

## Existing wheel

The uploaded `wheel.html` (428 lines) reads `/api/wheel/state`, polls every 700 ms, and uses the persisted `wheel_status`, `wheel_active_rank`, and `wheel_reveal_count` settings plus `draw_results`. Keep this live-draw state contract intact when the wheel is moved into the React application.

## Important behavior to preserve

- 100 numbers by default; price 100 ETB by default.
- 15-minute reservation/payment period.
- 20-minute draw delay after all numbers are sold.
- Three configured prizes (4,000 / 2,000 / 1,500 ETB).
- Payment is not considered sold until the host approves it.
- English and Amharic language preferences.
- Existing FOMO, user history, winner notifications, support-channel messages and draw sequence.

## First-slice limitations

The starter UI supports selecting and releasing numbers, viewing history, and an initial host approval list. It does not yet complete checkout, receipt storage, or the existing Telegram notification chain from the dashboard. Those are deliberate next steps and must be implemented before switching live users.


## Migration slice 2 implementation

- Added checkout API routes backed by the existing `begin_checkout`, `save_customer_name`, and `create_order` functions.
- Added the additive `miniapp_receipts` table for receipt metadata; no existing game/order table is reset or redefined.
- Added private Supabase Storage upload using a server-only service-role key. Accepted content is signature-checked JPEG, PNG or WebP up to 8 MB.
- Added owner/order/status/expiry checks before upload and a second order-state check before receipt metadata is committed. Duplicate receipt submissions are rejected.
- Added host-only signed receipt URL generation with a five-minute expiry.
- Added Telegram support-channel photo notification using the bot's existing approval callback payload format. If channel delivery fails, the host receives a fallback message where possible.
- Added host dashboard decision notifications to the customer via Telegram Bot API.
- Added Mini App checkout fields, invoice display, direct receipt upload and pending-order restoration after reload.
- Static Python compilation passed. Frontend production build was not run because frontend `node_modules` are not present in this environment. No live Supabase, Telegram or Render integration has been performed.
