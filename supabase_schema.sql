-- VisualAssistCam Supabase schema
-- Run this in the Supabase SQL editor.

create extension if not exists pgcrypto;

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

alter table public.users enable row level security;
alter table public.analysis_history enable row level security;
alter table public.usage_events enable row level security;

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
