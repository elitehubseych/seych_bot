-- Fix users.id: create sequence, populate NULL ids, set default nextval, set NOT NULL, add PK if missing
DO $$
BEGIN
    -- create sequence if not exists
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relkind = 'S' AND relname = 'users_id_seq') THEN
        CREATE SEQUENCE users_id_seq;
    END IF;

    -- set sequence to max(id) or 1
    PERFORM setval('users_id_seq', COALESCE((SELECT MAX(id) FROM public.users), 1));

    -- fill NULL ids with nextval
    UPDATE public.users SET id = nextval('users_id_seq') WHERE id IS NULL;

    -- ensure sequence is ahead of max(id)
    PERFORM setval('users_id_seq', GREATEST((SELECT MAX(id) FROM public.users), currval('users_id_seq')));

    -- set default if missing
    ALTER TABLE public.users ALTER COLUMN id SET DEFAULT nextval('users_id_seq');

    -- set NOT NULL
    ALTER TABLE public.users ALTER COLUMN id SET NOT NULL;

    -- add primary key if missing
    IF NOT EXISTS (
        SELECT 1 FROM pg_index i JOIN pg_class c ON i.indrelid = c.oid WHERE c.relname = 'users' AND i.indisprimary
    ) THEN
        ALTER TABLE public.users ADD PRIMARY KEY (id);
    END IF;
END$$;
