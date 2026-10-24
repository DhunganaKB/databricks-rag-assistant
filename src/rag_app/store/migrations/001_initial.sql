-- 001_initial: conversation history (short-term memory), long-term memory, feedback.
-- {schema} is replaced with the configured Postgres schema (default rag_memory).
-- Idempotent: safe to run more than once.

CREATE SCHEMA IF NOT EXISTS {schema};

-- SHORT-TERM MEMORY: one conversation and its turns ---------------------------------
CREATE TABLE IF NOT EXISTS {schema}.sessions (
  session_id        UUID PRIMARY KEY,
  user_id           TEXT NOT NULL,                       -- Databricks identity (SSO email)
  title             TEXT,
  status            TEXT NOT NULL DEFAULT 'active',      -- active | deleted
  turn_count        INT  NOT NULL DEFAULT 0,
  running_summary   TEXT NOT NULL DEFAULT '',            -- older turns, compressed
  summary_upto_turn INT  NOT NULL DEFAULT 0,             -- turns <= this are in the summary
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_active_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON {schema}.sessions (user_id, last_active_at DESC);

CREATE TABLE IF NOT EXISTS {schema}.messages (
  message_id        UUID PRIMARY KEY,
  session_id        UUID NOT NULL REFERENCES {schema}.sessions(session_id) ON DELETE CASCADE,
  turn_index        INT  NOT NULL,
  role              TEXT NOT NULL,                       -- user | assistant
  content           TEXT NOT NULL,
  client_message_id TEXT,                                -- idempotent retries
  standalone_query  TEXT,                                -- query actually searched
  citations         JSONB,                               -- incl. bounding boxes / spans
  request_id        TEXT,
  latency_ms        INT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (session_id, turn_index, role)
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_client_id_idx
  ON {schema}.messages (session_id, client_message_id) WHERE client_message_id IS NOT NULL;

-- LONG-TERM MEMORY: durable facts about a user, across sessions ----------------------
CREATE TABLE IF NOT EXISTS {schema}.user_memories (
  memory_id         UUID PRIMARY KEY,
  user_id           TEXT NOT NULL,
  memory_type       TEXT NOT NULL,                       -- profile | preference
  content           TEXT NOT NULL,                       -- "User works in the Rotterdam office."
  normalized        TEXT NOT NULL,                       -- de-duplication key
  source_session_id UUID,
  status            TEXT NOT NULL DEFAULT 'active',      -- active | deleted
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS user_memories_user_idx ON {schema}.user_memories (user_id, status, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS user_memories_dedup_idx
  ON {schema}.user_memories (user_id, normalized) WHERE status = 'active';

-- QUALITY LOOP --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS {schema}.feedback (
  feedback_id UUID PRIMARY KEY,
  user_id     TEXT NOT NULL,
  request_id  TEXT NOT NULL,
  rating      SMALLINT NOT NULL,
  comment     TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
