#!/usr/bin/env python3
"""Safely fix `users.id`: create sequence, populate null ids, set default, set NOT NULL, add PK if missing.

Usage:
  - Dry-run (default): show planned actions without applying
      python scripts/fix_users_id.py
  - Apply changes: set `DATABASE_URL` env var or pass --db and run with --apply
      DATABASE_URL="postgres://..." python scripts/fix_users_id.py --apply

This script performs checks before altering the table.
"""
from __future__ import annotations
import os
import sys
import argparse
import psycopg2
from psycopg2 import sql


def conn(database_url: str):
    return psycopg2.connect(database_url)


def has_primary_key(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM pg_index i JOIN pg_class c ON i.indrelid = c.oid WHERE c.relname = 'users' AND i.indisprimary;")
        return cur.fetchone()[0] > 0


def id_has_default(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT column_default FROM information_schema.columns WHERE table_schema='public' AND table_name='users' AND column_name='id';")
        row = cur.fetchone()
        return bool(row and row[0])


def id_has_nulls(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM public.users WHERE id IS NULL);")
        return cur.fetchone()[0]


def apply_fix(conn):
    with conn.cursor() as cur:
        # Determine id column type
        cur.execute("SELECT data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='users' AND column_name='id';")
        row = cur.fetchone()
        id_type = row[0] if row else None

        is_integer_like = id_type in ('bigint', 'integer', 'smallint')

        if is_integer_like:
            # integer-like id: create sequence, populate NULLs with nextval, set sequence value
            cur.execute("SELECT MAX(id) FROM public.users;")
            max_id = cur.fetchone()[0] or 0

            cur.execute("SELECT 1 FROM pg_class WHERE relkind = 'S' AND relname = 'users_id_seq';")
            if not cur.fetchone():
                cur.execute("CREATE SEQUENCE users_id_seq;")

            cur.execute(sql.SQL("SELECT setval(%s, %s, true);"), ('users_id_seq', max_id))

            cur.execute("UPDATE public.users SET id = nextval('users_id_seq') WHERE id IS NULL;")

            cur.execute("SELECT setval('users_id_seq', GREATEST((SELECT MAX(id) FROM public.users), currval('users_id_seq')));")

            cur.execute("ALTER TABLE public.users ALTER COLUMN id SET DEFAULT nextval('users_id_seq');")
        else:
            # non-integer id: set textual default so INSERTs succeed
            cur.execute("ALTER TABLE public.users ALTER COLUMN id SET DEFAULT (md5(random()::text || clock_timestamp()::text));")

        # set not null
        cur.execute("ALTER TABLE public.users ALTER COLUMN id SET NOT NULL;")

        # add primary key if missing
        cur.execute("SELECT COUNT(*) FROM pg_index i JOIN pg_class c ON i.indrelid = c.oid WHERE c.relname = 'users' AND i.indisprimary;")
        if cur.fetchone()[0] == 0:
            cur.execute("ALTER TABLE public.users ADD PRIMARY KEY (id);")

    conn.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--db', help='Database URL override')
    args = parser.parse_args()

    database_url = args.db or os.environ.get('DATABASE_URL')
    if not database_url:
        print('Error: set DATABASE_URL or pass --db', file=sys.stderr)
        sys.exit(2)

    try:
        c = conn(database_url)
    except Exception as e:
        print('Failed to connect:', e, file=sys.stderr)
        sys.exit(3)

    try:
        print('Connected. Inspecting current state...')
        pk = has_primary_key(c)
        default = id_has_default(c)
        nulls = id_has_nulls(c)
        print(f'Primary key present: {pk}')
        print(f'id has default: {default}')
        print(f'id has NULL values: {nulls}')

        if not args.apply:
            print('\nPlanned actions:')
            if not default:
                print('- Create sequence users_id_seq if missing; set default nextval for id')
            if nulls:
                print('- Populate NULL id rows with nextval from sequence')
            print('- Set id NOT NULL')
            if not pk:
                print('- Add PRIMARY KEY on id')
            print('\nRun with --apply to make changes (after confirming target DB).')
        else:
            print('Applying changes...')
            apply_fix(c)
            print('Done: id sequence/default/PK fixed.')
    finally:
        c.close()


if __name__ == '__main__':
    main()
