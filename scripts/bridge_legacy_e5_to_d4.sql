-- The old database reports e5f7a9c3b1d2, absent from this checkout.
-- Apply only after comparing its columns, constraints, indexes and enum labels
-- with d4f6a8c2e901. This script changes the revision marker, not the schema.
BEGIN;
DO $$
BEGIN
    IF (SELECT version_num FROM alembic_version) <> 'e5f7a9c3b1d2' THEN
        RAISE EXCEPTION 'Unexpected Alembic revision; legacy bridge aborted';
    END IF;
END $$;
UPDATE alembic_version
SET version_num = 'd4f6a8c2e901'
WHERE version_num = 'e5f7a9c3b1d2';
COMMIT;
