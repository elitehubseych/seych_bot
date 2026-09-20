#!/usr/bin/env python3
"""Check existence of users by vk_id in public.users.

Usage:
  python scripts/check_vk_users.py --db '<dsn>' --id 701235129 --id 532796366
"""
from __future__ import annotations
import os
import sys
import argparse
import psycopg2


def get_conn(dsn):
    return psycopg2.connect(dsn)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', help='Database DSN', required=False)
    parser.add_argument('--id', dest='ids', action='append', help='vk_id to check', required=True)
    args = parser.parse_args()
    dsn = args.db or os.environ.get('DATABASE_URL')
    if not dsn:
        print('Provide --db or set DATABASE_URL', file=sys.stderr)
        sys.exit(2)

    conn = get_conn(dsn)
    try:
        with conn.cursor() as cur:
            for vid in args.ids:
                try:
                    cur.execute('SELECT * FROM public.users WHERE vk_id = %s LIMIT 1', (int(vid),))
                except Exception:
                    # try as string
                    cur.execute('SELECT * FROM public.users WHERE vk_id = %s LIMIT 1', (vid,))
                row = cur.fetchone()
                if row:
                    print(f'FOUND vk_id={vid} -> id={row[0]}')
                else:
                    print(f'NOT FOUND vk_id={vid}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
