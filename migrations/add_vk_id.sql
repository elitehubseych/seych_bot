-- Safe SQL to ensure `vk_id` exists on `users` table
ALTER TABLE public.users
    ADD COLUMN IF NOT EXISTS vk_id BIGINT;

CREATE INDEX IF NOT EXISTS idx_users_vk_id ON public.users(vk_id);
