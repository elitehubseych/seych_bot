#!/usr/bin/env python3
"""Safe migration to add `vk_id` column to `users` table.

Usage:
  - Dry-run (default): prints actions without applying
      python scripts/migrate_add_vk_id.py
  - Apply changes: set `DATABASE_URL` env var and run with --apply
      DATABASE_URL="postgres://..." python scripts/migrate_add_vk_id.py --apply

This script checks information_schema first and only alters if missing.
"""
from __future__ import annotations
import os
import sys
import argparse
import psycopg2
from psycopg2 import sql


def get_conn(database_url: str):
    return psycopg2.connect(database_url)


def column_exists(conn, schema: str, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s AND column_name = %s
            """,
            (schema, table, column),
        )
        return cur.fetchone() is not None


def apply_migration(conn, schema: str = "public", table: str = "users"):
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("ALTER TABLE {}.{} ADD COLUMN IF NOT EXISTS vk_id BIGINT;")
            .format(sql.Identifier(schema), sql.Identifier(table))
        )
        cur.execute(
            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {}.{} (vk_id);")
            .format(sql.Identifier('idx_users_vk_id'), sql.Identifier(schema), sql.Identifier(table))
        )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Safe add vk_id migration")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    parser.add_argument("--db", help="Database URL override (env DATABASE_URL used if not set)")
    args = parser.parse_args()

    database_url = args.db or os.environ.get("DATABASE_URL")
    if not database_url:
        print("Error: set DATABASE_URL environment variable or pass --db", file=sys.stderr)
        sys.exit(2)

    print(f"Connecting to DB (dry-run={not args.apply})...")
    try:
        conn = get_conn(database_url)
    except Exception as e:
        print("Failed to connect:", e, file=sys.stderr)
        sys.exit(3)

    try:
        exists = column_exists(conn, 'public', 'users', 'vk_id')
        if exists:
            print("Column 'vk_id' already exists on public.users. No action needed.")
        else:
            print("Column 'vk_id' not found on public.users.")
            print("Planned SQL: ALTER TABLE public.users ADD COLUMN vk_id BIGINT; CREATE INDEX idx_users_vk_id ON public.users(vk_id);")
            if args.apply:
                print("Applying migration now...")
                apply_migration(conn)
                print("Migration applied: vk_id column added and index created.")
            else:
                print("Dry-run: to apply run with --apply after confirming target DB.")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
