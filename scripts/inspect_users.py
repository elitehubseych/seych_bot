#!/usr/bin/env python3
"""Inspect `public.users` schema and sample id values."""
from __future__ import annotations
import os
import sys
import argparse
import psycopg2


def get_conn(dsn):
    return psycopg2.connect(dsn)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', help='Database URL', required=False)
    args = parser.parse_args()
    dsn = args.db or os.environ.get('DATABASE_URL')
    if not dsn:
        print('Set DATABASE_URL or pass --db', file=sys.stderr)
        sys.exit(2)

    conn = get_conn(dsn)
    try:
        with conn.cursor() as cur:
            print('Columns for public.users:')
            cur.execute("SELECT column_name, data_type, column_default, is_nullable FROM information_schema.columns WHERE table_schema='public' AND table_name='users' ORDER BY ordinal_position;")
            for row in cur.fetchall():
                print(' ', row)

            cur.execute("SELECT COUNT(*) FROM public.users;")
            total = cur.fetchone()[0]
            print('\nTotal rows:', total)

            cur.execute("SELECT COUNT(*) FROM public.users WHERE id IS NULL;")
            nulls = cur.fetchone()[0]
            print('Rows with id IS NULL:', nulls)

            print('\nSample id values (first 20):')
            cur.execute("SELECT id FROM public.users LIMIT 20;")
            for r in cur.fetchall():
                print(' ', r[0])

            print('\nAttempting to compute MAX(id) as bigint (if possible)...')
            try:
                cur.execute("SELECT MAX(id::bigint) FROM public.users;")
                print(' MAX(id::bigint)=', cur.fetchone()[0])
            except Exception as e:
                print(' Could not cast id to bigint:', e)

    finally:
        conn.close()


if __name__ == '__main__':
    main()
