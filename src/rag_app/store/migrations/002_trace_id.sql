-- 002_trace_id: link each answer to its MLflow trace, so user feedback can be attached
-- to the trace of the answer it rates. Additive: the running app version keeps working.

ALTER TABLE {schema}.messages ADD COLUMN IF NOT EXISTS trace_id TEXT;
