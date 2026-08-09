-- ============================================================================
-- Invoice Checker — Supabase schema (INTERNAL build, auth disconnected)
-- Run this in the Supabase SQL Editor (Dashboard > SQL > New query).
--
-- Auth is disconnected for internal use, so `user_id` is a plain nullable
-- column (no auth.users FK, no Row Level Security). The backend writes/reads
-- rows with its service-role key and attributes them to DEFAULT_USER_ID.
--
-- To re-enable authentication later: set AUTH_REQUIRED=true on the backend and
-- swap to the commented "secure" variant at the bottom of this file.
-- ============================================================================

create extension if not exists "pgcrypto";

create table if not exists public.comparison_runs (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid,                       -- nullable; internal id when auth is off
    filename        text not null,
    run_at          timestamptz not null default now(),
    matched_count   integer not null default 0,
    mismatch_count  integer not null default 0,
    missing_count   integer not null default 0
);

create index if not exists comparison_runs_user_id_run_at_idx
    on public.comparison_runs (user_id, run_at desc);

-- RLS intentionally left DISABLED for the internal build. All access is
-- mediated by the backend service-role key.

-- ----------------------------------------------------------------------------
-- SECURE VARIANT (uncomment when AUTH_REQUIRED=true and you want per-user RLS):
--
-- alter table public.comparison_runs
--     add constraint comparison_runs_user_fk
--     foreign key (user_id) references auth.users (id) on delete cascade;
--
-- alter table public.comparison_runs enable row level security;
--
-- create policy "Users can read own comparison runs"
--     on public.comparison_runs for select using (auth.uid() = user_id);
-- create policy "Users can insert own comparison runs"
--     on public.comparison_runs for insert with check (auth.uid() = user_id);
-- create policy "Users can delete own comparison runs"
--     on public.comparison_runs for delete using (auth.uid() = user_id);
-- ----------------------------------------------------------------------------
