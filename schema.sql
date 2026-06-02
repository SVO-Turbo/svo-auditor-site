-- SVO Auditor — Supabase Schema
-- Run this in the Supabase SQL Editor after creating your project.

-- ─── Lead capture table ────────────────────────────────────────
-- One row per email captured. This is your marketing database.
create table if not exists audits (
  id           uuid primary key default gen_random_uuid(),
  email        text not null,
  domain       text not null,
  score        smallint not null,
  report_id    uuid,
  created_at   timestamptz not null default now()
);

create index if not exists idx_audits_score      on audits (score);
create index if not exists idx_audits_domain     on audits (domain);
create index if not exists idx_audits_email      on audits (email);
create index if not exists idx_audits_created_at on audits (created_at desc);

-- ─── Full report data (Tier 2 unlock) ─────────────────────────
-- Stores the complete check payload including fix/verify directives.
-- Only returned by /api/capture after a valid email is submitted.
-- Rows older than 30 days can be deleted via the cleanup job below.
create table if not exists reports (
  id           uuid primary key,
  domain       text not null,
  score        smallint not null,
  checks       jsonb not null,
  email        text,
  unlocked     boolean not null default false,
  created_at   timestamptz not null default now()
);

create index if not exists idx_reports_created_at on reports (created_at desc);
create index if not exists idx_reports_domain     on reports (domain);

-- ─── 30-day cleanup ──────────────────────────────────────────
-- Enable pg_cron in Supabase (Database → Extensions → pg_cron)
-- then run this to schedule daily cleanup at 03:00 UTC:
--
-- select cron.schedule(
--   'cleanup-old-reports',
--   '0 3 * * *',
--   $$ delete from reports where created_at < now() - interval '30 days' $$
-- );
