-- Lumen Supabase schema
-- Run this in the Supabase SQL editor.

create extension if not exists pgcrypto;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'lumen-assets',
    'lumen-assets',
    false,
    5242880,
    array['image/png', 'image/jpeg', 'image/webp', 'application/json']
)
on conflict (id) do update
set public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

create table if not exists public.users (
    id uuid primary key references auth.users(id) on delete cascade,
    email text,
    full_name text,
    avatar_url text,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.users (id, email, full_name, avatar_url)
    values (
        new.id,
        new.email,
        coalesce(new.raw_user_meta_data ->> 'full_name', ''),
        coalesce(new.raw_user_meta_data ->> 'avatar_url', '')
    )
    on conflict (id) do update
    set email = excluded.email,
        full_name = excluded.full_name,
        avatar_url = excluded.avatar_url,
        updated_at = timezone('utc', now());
    return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
after insert on auth.users
for each row execute procedure public.handle_new_user();

create table if not exists public.analysis_history (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    subject text,
    teacher text,
    session_id text,
    board_id integer,
    topic text,
    analysis_text text not null,
    ocr_text text,
    ai_response text,
    board_svg text,
    image_path text,
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists analysis_history_user_created_idx
    on public.analysis_history (user_id, created_at desc);

create table if not exists public.usage_events (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    event_type text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists usage_events_user_created_idx
    on public.usage_events (user_id, created_at desc);

create table if not exists public.class_sessions (
    id text primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    subject text,
    teacher text,
    created_at timestamptz not null default timezone('utc', now()),
    locked boolean not null default false,
    captures jsonb not null default '[]'::jsonb
);

create index if not exists class_sessions_user_created_idx
    on public.class_sessions (user_id, created_at desc);

create table if not exists public.user_quota_overrides (
    user_id uuid primary key references auth.users(id) on delete cascade,
    daily_analyses_limit integer,
    monthly_analyses_limit integer,
    daily_upload_limit integer,
    notes text,
    updated_at timestamptz not null default timezone('utc', now())
);

create table if not exists public.admin_surveys (
    id uuid primary key default gen_random_uuid(),
    created_by uuid references auth.users(id) on delete set null,
    target_user_id uuid references auth.users(id) on delete set null,
    target_email text,
    title text not null,
    feature_key text,
    description text,
    status text not null default 'draft',
    created_at timestamptz not null default timezone('utc', now())
);

grant usage on schema public to anon, authenticated, service_role;
grant select, insert, update on public.users to authenticated;
grant select, insert, delete on public.analysis_history to authenticated;
grant select, insert on public.usage_events to authenticated;
grant select, insert, update, delete on public.class_sessions to authenticated;
grant select on public.user_quota_overrides to authenticated;
grant all on public.users to service_role;
grant all on public.analysis_history to service_role;
grant all on public.usage_events to service_role;
grant all on public.class_sessions to service_role;
grant all on public.user_quota_overrides to service_role;
grant all on public.admin_surveys to service_role;

alter table public.users enable row level security;
alter table public.analysis_history enable row level security;
alter table public.usage_events enable row level security;
alter table public.class_sessions enable row level security;
alter table public.user_quota_overrides enable row level security;
alter table public.admin_surveys enable row level security;

drop policy if exists "Users can view their own profile" on public.users;
create policy "Users can view their own profile"
on public.users
for select
using (auth.uid() = id);

drop policy if exists "Users can update their own profile" on public.users;
create policy "Users can update their own profile"
on public.users
for update
using (auth.uid() = id)
with check (auth.uid() = id);

drop policy if exists "Users can insert their own profile" on public.users;
create policy "Users can insert their own profile"
on public.users
for insert
with check (auth.uid() = id);

drop policy if exists "Users can view their own analyses" on public.analysis_history;
create policy "Users can view their own analyses"
on public.analysis_history
for select
using (auth.uid() = user_id);

drop policy if exists "Users can insert their own analyses" on public.analysis_history;
create policy "Users can insert their own analyses"
on public.analysis_history
for insert
with check (auth.uid() = user_id);

drop policy if exists "Users can delete their own analyses" on public.analysis_history;
create policy "Users can delete their own analyses"
on public.analysis_history
for delete
using (auth.uid() = user_id);

drop policy if exists "Users can view their own usage events" on public.usage_events;
create policy "Users can view their own usage events"
on public.usage_events
for select
using (auth.uid() = user_id);

drop policy if exists "Users can insert their own usage events" on public.usage_events;
create policy "Users can insert their own usage events"
on public.usage_events
for insert
with check (auth.uid() = user_id);

drop policy if exists "Users can view their own class sessions" on public.class_sessions;
create policy "Users can view their own class sessions"
on public.class_sessions
for select
using (auth.uid() = user_id);

drop policy if exists "Users can insert their own class sessions" on public.class_sessions;
create policy "Users can insert their own class sessions"
on public.class_sessions
for insert
with check (auth.uid() = user_id);

drop policy if exists "Users can update their own class sessions" on public.class_sessions;
create policy "Users can update their own class sessions"
on public.class_sessions
for update
using (auth.uid() = user_id)
with check (auth.uid() = user_id);

drop policy if exists "Users can delete their own class sessions" on public.class_sessions;
create policy "Users can delete their own class sessions"
on public.class_sessions
for delete
using (auth.uid() = user_id);

drop policy if exists "Service role manages quota overrides" on public.user_quota_overrides;
create policy "Service role manages quota overrides"
on public.user_quota_overrides
for all
using (auth.role() = 'service_role')
with check (auth.role() = 'service_role');

drop policy if exists "Users can view their own quota override" on public.user_quota_overrides;
create policy "Users can view their own quota override"
on public.user_quota_overrides
for select
using (auth.uid() = user_id);

drop policy if exists "Service role manages admin surveys" on public.admin_surveys;
create policy "Service role manages admin surveys"
on public.admin_surveys
for all
using (auth.role() = 'service_role')
with check (auth.role() = 'service_role');
