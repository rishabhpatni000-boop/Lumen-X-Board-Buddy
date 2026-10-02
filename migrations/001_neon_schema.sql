-- Lumen application schema for Neon Postgres.
-- Authentication identities live in neon_auth; application ownership uses
-- public.users.id so legacy Supabase data can be linked by email safely.

create extension if not exists pgcrypto;

create table if not exists public.users (
    id uuid primary key default gen_random_uuid(),
    neon_auth_user_id uuid unique references neon_auth."user"(id) on delete set null,
    legacy_supabase_user_id uuid unique,
    email text not null unique,
    full_name text not null default '',
    avatar_url text not null default '',
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create or replace function public.handle_new_neon_user()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
    insert into public.users (
        neon_auth_user_id,
        email,
        full_name,
        avatar_url
    )
    values (
        new.id,
        lower(new.email),
        coalesce(new.name, ''),
        coalesce(new.image, '')
    )
    on conflict (email) do update
    set neon_auth_user_id = excluded.neon_auth_user_id,
        full_name = case
            when excluded.full_name <> '' then excluded.full_name
            else public.users.full_name
        end,
        avatar_url = case
            when excluded.avatar_url <> '' then excluded.avatar_url
            else public.users.avatar_url
        end,
        updated_at = timezone('utc', now());
    return new;
end;
$$;

drop trigger if exists on_neon_auth_user_created on neon_auth."user";
create trigger on_neon_auth_user_created
after insert or update of email, name, image on neon_auth."user"
for each row execute procedure public.handle_new_neon_user();

create table if not exists public.analysis_history (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references public.users(id) on delete cascade,
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
    user_id uuid not null references public.users(id) on delete cascade,
    event_type text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists usage_events_user_created_idx
    on public.usage_events (user_id, created_at desc);

create table if not exists public.class_sessions (
    id text primary key,
    user_id uuid not null references public.users(id) on delete cascade,
    subject text,
    teacher text,
    created_at timestamptz not null default timezone('utc', now()),
    locked boolean not null default false,
    captures jsonb not null default '[]'::jsonb
);

create index if not exists class_sessions_user_created_idx
    on public.class_sessions (user_id, created_at desc);

create table if not exists public.user_quota_overrides (
    user_id uuid primary key references public.users(id) on delete cascade,
    daily_analyses_limit integer,
    monthly_analyses_limit integer,
    daily_upload_limit integer,
    notes text,
    updated_at timestamptz not null default timezone('utc', now())
);

create table if not exists public.admin_surveys (
    id uuid primary key default gen_random_uuid(),
    created_by uuid references public.users(id) on delete set null,
    target_user_id uuid references public.users(id) on delete set null,
    target_email text,
    title text not null,
    feature_key text,
    description text,
    status text not null default 'draft',
    created_at timestamptz not null default timezone('utc', now())
);

comment on table public.users is
    'Stable Lumen users linked to Neon Auth and, during migration, legacy Supabase identities.';

