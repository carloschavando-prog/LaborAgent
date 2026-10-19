-- Additive; existing reports remain readable during rollout.
ALTER TABLE public.labor_daily
  ADD COLUMN IF NOT EXISTS import_status text NOT NULL DEFAULT 'unverified',
  ADD COLUMN IF NOT EXISTS import_note text,
  ADD COLUMN IF NOT EXISTS source_checked_at timestamptz;

ALTER TABLE public.labor_daily
  ADD CONSTRAINT labor_daily_import_status_check
  CHECK (import_status IN ('unverified', 'complete', 'pending'));
