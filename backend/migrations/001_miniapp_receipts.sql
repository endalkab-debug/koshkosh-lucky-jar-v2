-- Additive migration for Mini App payment receipts.
-- Existing game, cart, order, draw and user tables are not modified.
CREATE TABLE IF NOT EXISTS miniapp_receipts (
    order_id TEXT PRIMARY KEY REFERENCES orders(order_id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    storage_path TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
    file_size BIGINT NOT NULL CHECK (file_size > 0 AND file_size <= 8388608),
    uploaded_at TEXT NOT NULL,
    notification_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (notification_status IN ('pending', 'sent', 'pending_retry'))
);
