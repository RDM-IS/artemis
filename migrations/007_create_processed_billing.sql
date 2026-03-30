CREATE TABLE IF NOT EXISTS acos.processed_billing (
    message_id TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ DEFAULT now()
);
