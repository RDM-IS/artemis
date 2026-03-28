CREATE TABLE IF NOT EXISTS acos.action_items (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at      TIMESTAMPTZ DEFAULT now(),
  updated_at      TIMESTAMPTZ DEFAULT now(),
  item_type       VARCHAR(100) NOT NULL,
  status          VARCHAR(50) DEFAULT 'pending',
  priority        VARCHAR(20) DEFAULT 'normal',
  title           TEXT NOT NULL,
  description     TEXT,
  metadata        JSONB DEFAULT '{}',
  due_at          TIMESTAMPTZ,
  snoozed_until   TIMESTAMPTZ,
  reminder_count  INTEGER DEFAULT 0,
  last_reminded_at TIMESTAMPTZ,
  resolved_at     TIMESTAMPTZ,
  resolved_by     VARCHAR(100)
);

CREATE INDEX ON acos.action_items (status, item_type);
CREATE INDEX ON acos.action_items (due_at) WHERE status = 'pending';
