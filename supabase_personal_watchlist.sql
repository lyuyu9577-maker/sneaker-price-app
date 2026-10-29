-- Run once in the Supabase SQL Editor for this app.
create table if not exists public.personal_watchlist (
    user_id text not null,
    platform text not null check (platform in ('PChome', 'momo購物網')),
    product_id text not null,
    query text not null,
    title text not null,
    url text not null,
    added_at timestamptz not null default now(),
    primary key (user_id, platform, product_id)
);

-- Only the Streamlit server's service-role key may access personal lists.
alter table public.personal_watchlist enable row level security;
revoke all on public.personal_watchlist from anon, authenticated;
grant select, insert, delete on public.personal_watchlist to service_role;

-- Enforce five items atomically, including simultaneous browser sessions.
create or replace function public.add_personal_tracking(
    p_user_id text, p_platform text, p_product_id text,
    p_query text, p_title text, p_url text
) returns void
language plpgsql security definer set search_path = public
as $$
begin
    if auth.role() <> 'service_role' then
        raise exception 'service_role_required';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(p_user_id, 0));
    if exists (select 1 from public.personal_watchlist
               where user_id = p_user_id and platform = p_platform
                 and product_id = p_product_id) then
        return;
    end if;
    if (select count(*) from public.personal_watchlist where user_id = p_user_id) >= 5 then
        raise exception 'personal_tracking_limit_reached';
    end if;
    insert into public.personal_watchlist (user_id, platform, product_id, query, title, url)
    values (p_user_id, p_platform, p_product_id, p_query, p_title, p_url);
end;
$$;
revoke all on function public.add_personal_tracking(text,text,text,text,text,text) from public, anon, authenticated;
grant execute on function public.add_personal_tracking(text,text,text,text,text,text) to service_role;
