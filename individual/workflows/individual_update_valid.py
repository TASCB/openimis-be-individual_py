import logging

from core.models import User
from individual.workflows.utils import SqlProcedurePythonWorkflow
from individual.services import IndividualImportService

logger = logging.getLogger(__name__)


def process_update_valid_individuals_workflow(user_uuid, upload_uuid, accepted=None):
    user = User.objects.get(id=user_uuid)
    service = SqlProcedurePythonWorkflow(upload_uuid, user_uuid, accepted)
    service.validate_dataframe_headers(True)  # ID is required for updates

    if isinstance(accepted, list):
        service.execute(upload_sql_partial, [upload_uuid, user_uuid, accepted])
    else:
        service.execute(upload_sql, [upload_uuid, user_uuid])

    # Optional reporting sync
    IndividualImportService(user).synchronize_data_for_reporting(upload_uuid)

    # Optional: keep group wiring consistent on updates (safe no-op if nothing changed)
    try:
        IndividualImportService(user).link_groups_for_upload_uuid(str(upload_uuid))
    except Exception as e:
        logger.exception("Group linking (update) failed for upload %s: %s", upload_uuid, e)


# --------------------------
# FULL UPDATE (all rows)
# --------------------------
upload_sql = """
-- NOTE:
-- 1) We MERGE Json_ext (existing || incoming) to avoid erasing system-added keys.
-- 2) We guard against external_id collisions (when provided) with other active Individuals.
-- 3) We compute SUCCESS/PARTIAL_SUCCESS status based on valid rows in this upload.

DO $$
DECLARE
    current_upload_id UUID := %s::UUID;
    userUUID UUID := %s::UUID;
    failing_invalid_id UUID[];
    total_entries INT;
    total_valid_entries INT;
BEGIN
    -- Any datasource rows whose Json_ext.ID does not map to an existing Individual UUID?
    SELECT ARRAY_AGG(ds."UUID") INTO failing_invalid_id
    FROM individual_individualdatasource ds
    WHERE ds.upload_id = current_upload_id
      AND ds."isDeleted" = False
      AND COALESCE(ds.validations ->> 'validation_errors', '[]') = '[]'
      AND (
           NULLIF(btrim(ds."Json_ext"->>'ID'), '') IS NULL
           OR NOT EXISTS (
               SELECT 1 FROM individual_individual ii
               WHERE ii."UUID" = (ds."Json_ext"->>'ID')::UUID
                 AND ii."isDeleted" = False
           )
      );

    IF failing_invalid_id IS NOT NULL THEN
        UPDATE individual_individualdatasourceupload
        SET error = coalesce(error, '{}'::jsonb) || jsonb_build_object(
                'errors', jsonb_build_object(
                    'error', 'Invalid entries (missing/unknown ID)',
                    'timestamp', NOW()::text,
                    'upload_id', current_upload_id::text,
                    'failing_entries_invalid_id', failing_invalid_id
                )
            ),
            status = 'FAIL'
        WHERE "UUID" = current_upload_id;
        RETURN;
    END IF;

    -- Perform updates with safety checks
    WITH updated_individuals AS (
        UPDATE individual_individual ii
        SET first_name   = COALESCE(ids."Json_ext"->>'first_name', ii.first_name),
            last_name    = COALESCE(ids."Json_ext"->>'last_name',  ii.last_name),
            dob          = COALESCE(to_date(ids."Json_ext"->>'dob', 'YYYY-MM-DD'), ii.dob),
            location_id  = COALESCE(loc."LocationId", ii.location_id),
            "DateUpdated"= NOW(),
            -- MERGE JSON: keep existing keys unless overridden by incoming; drop explicit nulls
            "Json_ext"   = jsonb_strip_nulls(ii."Json_ext" || ids."Json_ext")
        FROM individual_individualdatasource ids
        LEFT JOIN "tblLocations" AS loc
                 ON loc."LocationName" = ds."Json_ext"->>'location_name'
                AND loc."LocationCode" = lpad(ds."Json_ext"->>'location_code', 9, '0')
                AND loc."LocationType" = 'V'
                AND loc."ValidityTo" IS NULL
        WHERE ids.upload_id = current_upload_id
          AND ids."isDeleted" = False
          AND COALESCE(ids.validations ->> 'validation_errors', '[]') = '[]'
          AND ii."UUID" = (ids."Json_ext"->>'ID')::UUID
          -- external_id uniqueness guard (only when provided)
          AND NOT EXISTS (
                SELECT 1
                FROM individual_individual i2
                WHERE i2."isDeleted" = False
                  AND i2."UUID" <> ii."UUID"
                  AND lower(NULLIF(btrim(i2."Json_ext"->>'external_id'), '')) =
                      lower(NULLIF(btrim(ids."Json_ext"->>'external_id'), ''))
                  AND NULLIF(btrim(ids."Json_ext"->>'external_id'), '') IS NOT NULL
          )
        RETURNING ii."UUID", ids."UUID" AS individualdatasource_id
    )
    UPDATE individual_individualdatasource ds
    SET individual_id = u."UUID"
    FROM updated_individuals u
    WHERE ds.upload_id = current_upload_id
      AND ds."UUID" = u.individualdatasource_id
      AND ds."isDeleted" = False
      AND ds.individual_id IS NULL;

    -- Summaries (scope = entire upload)
    SELECT count(*) INTO total_valid_entries
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id
      AND "isDeleted" = FALSE
      AND COALESCE(validations ->> 'validation_errors', '[]') = '[]';

    SELECT count(*) INTO total_entries
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id
      AND "isDeleted" = FALSE;

    UPDATE individual_individualdatasourceupload
    SET 
        status = CASE
            WHEN total_valid_entries = total_entries THEN 'SUCCESS'
            ELSE 'PARTIAL_SUCCESS'
        END,
        error = CASE
            WHEN total_valid_entries < total_entries THEN jsonb_build_object(
                'error', 'Partial success due to some invalid entries',
                'timestamp', NOW()::text,
                'upload_id', current_upload_id::text,
                'total_valid_entries', total_valid_entries,
                'total_entries', total_entries
            )
            ELSE '{}'
        END
    WHERE "UUID" = current_upload_id;

EXCEPTION WHEN OTHERS THEN
    UPDATE individual_individualdatasourceupload
    SET status = 'FAIL',
        error  = coalesce(error, '{}'::jsonb) || jsonb_build_object(
                    'error', SQLERRM,
                    'timestamp', NOW()::text,
                    'upload_id', current_upload_id::text
                 )
    WHERE "UUID" = current_upload_id;
END $$;
"""

# --------------------------
# PARTIAL UPDATE (accepted[] subset)
# --------------------------
upload_sql_partial = """
DO $$
DECLARE
    current_upload_id UUID := %s::UUID;
    userUUID UUID := %s::UUID;
    accepted UUID[] := %s::UUID[];
    failing_invalid_id UUID[];
    total_entries INT;
    total_valid_entries INT;
BEGIN
    -- Validate IDs only within ACCEPTED subset
    SELECT ARRAY_AGG(ds."UUID") INTO failing_invalid_id
    FROM individual_individualdatasource ds
    WHERE ds.upload_id = current_upload_id
      AND ds."UUID" = ANY(accepted)
      AND ds."isDeleted" = False
      AND COALESCE(ds.validations ->> 'validation_errors', '[]') = '[]'
      AND (
           NULLIF(btrim(ds."Json_ext"->>'ID'), '') IS NULL
           OR NOT EXISTS (
               SELECT 1 FROM individual_individual ii
               WHERE ii."UUID" = (ds."Json_ext"->>'ID')::UUID
                 AND ii."isDeleted" = False
           )
      );

    IF failing_invalid_id IS NOT NULL THEN
        UPDATE individual_individualdatasourceupload
        SET error = coalesce(error, '{}'::jsonb) || jsonb_build_object(
                'errors', jsonb_build_object(
                    'error', 'Invalid entries (missing/unknown ID)',
                    'timestamp', NOW()::text,
                    'upload_id', current_upload_id::text,
                    'failing_entries_invalid_id', failing_invalid_id
                )
            ),
            status = 'FAIL'
        WHERE "UUID" = current_upload_id;
        RETURN;
    END IF;

    -- Perform updates for ACCEPTED subset
    WITH updated_individuals AS (
        UPDATE individual_individual ii
        SET first_name   = COALESCE(ids."Json_ext"->>'first_name', ii.first_name),
            last_name    = COALESCE(ids."Json_ext"->>'last_name',  ii.last_name),
            dob          = COALESCE(to_date(ids."Json_ext"->>'dob', 'YYYY-MM-DD'), ii.dob),
            location_id  = COALESCE(loc."LocationId", ii.location_id),
            "DateUpdated"= NOW(),
            "Json_ext"   = jsonb_strip_nulls(ii."Json_ext" || ids."Json_ext")
        FROM individual_individualdatasource ids
        LEFT JOIN "tblLocations" AS loc
            ON loc."LocationName" = ds."Json_ext"->>'location_name'
            AND loc."LocationCode" = lpad(ds."Json_ext"->>'location_code', 9, '0')
            AND loc."LocationType" = 'V'
            AND loc."ValidityTo" IS NULL
        WHERE ids.upload_id = current_upload_id
          AND ids."UUID" = ANY(accepted)
          AND ids."isDeleted" = False
          AND COALESCE(ids.validations ->> 'validation_errors', '[]') = '[]'
          AND ii."UUID" = (ids."Json_ext"->>'ID')::UUID
          AND NOT EXISTS (
                SELECT 1
                FROM individual_individual i2
                WHERE i2."isDeleted" = False
                  AND i2."UUID" <> ii."UUID"
                  AND lower(NULLIF(btrim(i2."Json_ext"->>'external_id'), '')) =
                      lower(NULLIF(btrim(ids."Json_ext"->>'external_id'), ''))
                  AND NULLIF(btrim(ids."Json_ext"->>'external_id'), '') IS NOT NULL
          )
        RETURNING ii."UUID", ids."UUID" AS individualdatasource_id
    )
    UPDATE individual_individualdatasource ds
    SET individual_id = u."UUID"
    FROM updated_individuals u
    WHERE ds.upload_id = current_upload_id
      AND ds."UUID" = u.individualdatasource_id
      AND ds."isDeleted" = False
      AND ds.individual_id IS NULL;

    -- Summaries (scope = ACCEPTED subset only)
    SELECT count(*) INTO total_valid_entries
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id
      AND "isDeleted" = FALSE
      AND "UUID" = ANY(accepted)
      AND COALESCE(validations ->> 'validation_errors', '[]') = '[]';

    SELECT count(*) INTO total_entries
    FROM individual_individualdatasource
    WHERE upload_id = current_upload_id
      AND "isDeleted" = FALSE
      AND "UUID" = ANY(accepted);

    UPDATE individual_individualdatasourceupload
    SET 
        status = CASE
            WHEN total_valid_entries = total_entries THEN 'SUCCESS'
            ELSE 'PARTIAL_SUCCESS'
        END,
        error = CASE
            WHEN total_valid_entries < total_entries THEN jsonb_build_object(
                'error', 'Partial success due to some invalid entries (accepted subset)',
                'timestamp', NOW()::text,
                'upload_id', current_upload_id::text,
                'total_valid_entries', total_valid_entries,
                'total_entries', total_entries
            )
            ELSE '{}'
        END
    WHERE "UUID" = current_upload_id;

EXCEPTION WHEN OTHERS THEN
    UPDATE individual_individualdatasourceupload
    SET status = 'FAIL',
        error  = coalesce(error, '{}'::jsonb) || jsonb_build_object(
                    'error', SQLERRM,
                    'timestamp', NOW()::text,
                    'upload_id', current_upload_id::text
                 )
    WHERE "UUID" = current_upload_id;
END $$;
"""