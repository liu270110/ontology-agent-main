-- M0: extensions only; role/admin seeds live exclusively in Alembic data migrations (arch 08 s2.2).
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
-- vector: available once image swaps to pgvector/pgvector:pg16 (M2); non-fatal meanwhile.
DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'vector extension unavailable on this image; skipped (M2 will swap image)';
END $$;
