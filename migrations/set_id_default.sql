-- Set a safe textual default for users.id using md5(random()||clock_timestamp())
ALTER TABLE public.users ALTER COLUMN id SET DEFAULT (md5(random()::text || clock_timestamp()::text));
