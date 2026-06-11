import logging

from core.models import User
from core.utils import set_current_user, clear_current_user
from individual.workflows.utils import SqlProcedurePythonWorkflow
from individual.services import IndividualImportService

logger = logging.getLogger(__name__)


def process_import_valid_individuals_workflow(user_uuid, upload_uuid, accepted=None):
    """
    Approve 'valid' rows in a given upload:
      - Insert brand-new Individuals
      - Link duplicates (by json_ext.external_id) to existing Individuals
      - Synchronize reporting
      - Link Groups by group_code / hhrep (HEAD/PRIMARY)
    """
    user = User.objects.get(id=user_uuid)
    set_current_user(user)
    try:
        service = SqlProcedurePythonWorkflow(upload_uuid, user_uuid, accepted)
        service.validate_dataframe_headers()

        if isinstance(accepted, list):
            service.execute(upload_sql_partial, [upload_uuid, user_uuid, accepted])
        else:
            service.execute(upload_sql, [upload_uuid, user_uuid])

        # ---- reporting sync: NON-FATAL ----
        try:
            IndividualImportService(user).synchronize_data_for_reporting(upload_uuid)
        except Exception as e:
            logger.warning(
                "Reporting sync failed for upload %s (ignored for ETL): %s",
                upload_uuid,
                e,
                exc_info=True,
            )

        # ---- group wiring: MUST ALWAYS RUN ----
        try:
            IndividualImportService(user).link_groups_for_upload_uuid(str(upload_uuid))
        except Exception as e:
            logger.exception("Group linking failed for upload %s: %s", upload_uuid, e)

    finally:
        # CLEANUP: Clear current user after workflow completes
        clear_current_user()


# INSERT all valid rows; then also link duplicates by external_id to existing Individuals
upload_sql = """
DO $$
DECLARE
    current_upload_id UUID := %s::UUID;
    userUUID UUID := %s::UUID;
    failing_entries UUID[];
    total_entries INT;
    total_valid_entries INT;
BEGIN
    -- Isolate rows missing a REQUIRED value (first_name / last_name / dob).
    --
    -- The previous check used the jsonb key-presence operator
    -- (NOT "Json_ext" ? 'last_name'), which only catches a MISSING key. The
    -- Survey Solutions ETL always writes a last_name column, so a single-name
    -- member arrives with the key PRESENT but the value NULL/''. That slipped
    -- past the check, reached the INSERT below, and tripped the
    -- individual_individual.last_name NOT NULL constraint -- which the EXCEPTION
    -- handler then turned into status='FAIL' for the WHOLE upload (one blank
    -- surname aborted the entire district).
    --
    -- We now treat empty/whitespace as missing and FLAG those rows as invalid
    -- (write a validation_errors entry) instead of failing the whole upload.
    -- Flagged rows are excluded from the INSERT below, so the rest of the batch
    -- still imports (PARTIAL_SUCCESS) and the bad rows stay in staging for
    -- review/correction at source. No values are fabricated.
    UPDATE individual_individualdatasource ds
    SET validations = COALESCE(ds.validations, '{}'::jsonb) || jsonb_build_object(
            'validation_errors', jsonb_build_array(
                jsonb_build_object(
                    'error', 'Missing required field (first_name, last_name or dob)',
                    'field_validation', 'required_fields'
                )
            )
        )
    WHERE ds.upload_id = current_upload_id
      AND ds.individual_id IS NULL
      AND ds."isDeleted" = False
      AND COALESCE(ds.validations ->> 'validation_errors', '[]') = '[]'
      AND (
            NULLIF(btrim(ds."Json_ext"->>'first_name'), '') IS NULL
         OR NULLIF(btrim(ds."Json_ext"->>'last_name'), '') IS NULL
         OR NULLIF(btrim(ds."Json_ext"->>'dob'), '') IS NULL
      );

    -- Capture the held-back UUIDs for review (does NOT fail the upload).
    SELECT ARRAY_AGG(ds."UUID") INTO failing_entries
    FROM individual_individualdatasource ds
    WHERE ds.upload_id = current_upload_id
      AND ds.individual_id IS NULL
      AND ds."isDeleted" = False
      AND COALESCE(ds.validations ->> 'validation_errors', '[]') <> '[]';

    -- 1) INSERT brand-new Individuals (skip if external_id already exists)
    WITH new_entry AS (
        INSERT INTO individual_individual(
            "UUID", "isDeleted", version, "UserCreatedUUID", "UserUpdatedUUID",
            "Json_ext", first_name, last_name, dob, location_id
        )
        SELECT gen_random_uuid(), false, 1, userUUID, userUUID,
               ds."Json_ext",
               ds."Json_ext"->>'first_name',
               ds."Json_ext"->>'last_name',
               to_date(ds."Json_ext"->>'dob', 'YYYY-MM-DD'),
               loc."LocationId"
        FROM individual_individualdatasource AS ds
        LEFT JOIN "tblLocations" AS loc
            ON loc."LocationCode" = lpad(ds."Json_ext"->>'location_code', 9, '0')
            AND loc."LocationType" = 'V'
            AND loc."ValidityTo" IS NULL
        WHERE ds.upload_id = current_upload_id
          AND ds.individual_id IS NULL
          AND ds."isDeleted" = False
          AND COALESCE(ds.validations ->> 'validation_errors', '[]') = '[]'
          AND NOT EXISTS (
                SELECT 1
                FROM individual_individual i2
                WHERE i2."isDeleted" = False
                  AND lower(NULLIF(btrim(i2."Json_ext"->>'external_id'), '')) =
                      lower(NULLIF(btrim(ds."Json_ext"->>'external_id'), ''))
                  AND NULLIF(btrim(ds."Json_ext"->>'external_id'), '') IS NOT NULL
          )
        RETURNING "UUID", "Json_ext"
    )
    UPDATE individual_individualdatasource AS ids
    SET individual_id = ne."UUID"
    FROM new_entry ne
    WHERE ids.upload_id = current_upload_id
      AND ids.individual_id IS NULL
      AND ids."isDeleted" = False
      AND ids."Json_ext" = ne."Json_ext"
      AND COALESCE(ids.validations ->> 'validation_errors', '[]') = '[]';

    -- 2) LINK duplicates by external_id to existing Individuals (no insert, but set individual_id)
    UPDATE individual_individualdatasource AS ids
    SET individual_id = i2."UUID"
    FROM individual_individual i2
    WHERE ids.upload_id = current_upload_id
      AND ids.individual_id IS NULL
      AND ids."isDeleted" = False
      AND COALESCE(ids.validations ->> 'validation_errors', '[]') = '[]'
      AND NULLIF(btrim(ids."Json_ext"->>'external_id'), '') IS NOT NULL
      AND i2."isDeleted" = False
      AND lower(NULLIF(btrim(i2."Json_ext"->>'external_id'), '')) =
          lower(NULLIF(btrim(ids."Json_ext"->>'external_id'), ''));

    -- Status summary: valid rows imported, invalid rows held back for review.
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
            WHEN total_valid_entries = 0 THEN 'FAIL'
            WHEN total_valid_entries = total_entries THEN 'SUCCESS'
            ELSE 'PARTIAL_SUCCESS'
        END,
        error = CASE
            WHEN total_valid_entries < total_entries THEN jsonb_build_object(
                'error', 'Partial success: rows with a missing required field (first_name, last_name or dob) or a validation error were skipped and left in staging for review',
                'timestamp', NOW()::text,
                'upload_id', current_upload_id::text,
                'total_valid_entries', total_valid_entries,
                'total_entries', total_entries,
                'failing_entries', failing_entries
            )
            ELSE '{}'
        END
    WHERE "UUID" = current_upload_id;
EXCEPTION WHEN OTHERS THEN
    UPDATE individual_individualdatasourceupload
    SET status = 'FAIL',
        error = jsonb_build_object(
            'error', SQLERRM,
            'timestamp', NOW()::text,
            'upload_id', current_upload_id::text
        )
    WHERE "UUID" = current_upload_id;
END $$;
"""


upload_sql_partial = """
DO $$
DECLARE
    current_upload_id UUID := %s::UUID;
    userUUID UUID := %s::UUID;
    accepted UUID[] := %s::UUID[];
BEGIN
    -- Flag accepted rows missing a REQUIRED value (first_name / last_name / dob)
    -- using value-emptiness, not jsonb key-presence (see upload_sql above for the
    -- full rationale). Flagged rows are excluded from the INSERT below so the
    -- rest of the approved batch still imports instead of the whole upload
    -- aborting on the NOT NULL constraint.
    UPDATE individual_individualdatasource ds
    SET validations = COALESCE(ds.validations, '{}'::jsonb) || jsonb_build_object(
            'validation_errors', jsonb_build_array(
                jsonb_build_object(
                    'error', 'Missing required field (first_name, last_name or dob)',
                    'field_validation', 'required_fields'
                )
            )
        )
    WHERE ds.upload_id = current_upload_id
      AND ds.individual_id IS NULL
      AND ds."isDeleted" = False
      AND COALESCE(ds.validations ->> 'validation_errors', '[]') = '[]'
      AND (accepted IS NULL OR ds."UUID" = ANY(accepted))
      AND (
            NULLIF(btrim(ds."Json_ext"->>'first_name'), '') IS NULL
         OR NULLIF(btrim(ds."Json_ext"->>'last_name'), '') IS NULL
         OR NULLIF(btrim(ds."Json_ext"->>'dob'), '') IS NULL
      );

        -- 1) INSERT brand-new Individuals (only accepted rows)
        WITH new_entry AS (
            INSERT INTO individual_individual(
                "UUID", "isDeleted", version, "UserCreatedUUID", "UserUpdatedUUID",
                "Json_ext", first_name, last_name, dob, location_id
            )
            SELECT gen_random_uuid(), false, 1, userUUID, userUUID,
                   ds."Json_ext",
                   ds."Json_ext"->>'first_name',
                   ds."Json_ext"->>'last_name',
                   to_date(ds."Json_ext"->>'dob', 'YYYY-MM-DD'),
                   loc."LocationId"
            FROM individual_individualdatasource AS ds
            LEFT JOIN "tblLocations" AS loc
                    ON loc."LocationCode" = lpad(ds."Json_ext"->>'location_code', 9, '0')
                    AND loc."LocationType" = 'V'
                    AND loc."ValidityTo" IS NULL
            WHERE ds.upload_id = current_upload_id 
              AND ds.individual_id IS NULL
              AND ds."isDeleted" = False
              AND COALESCE(ds.validations ->> 'validation_errors', '[]') = '[]'
              AND (accepted IS NULL OR ds."UUID" = ANY(accepted))
              AND NOT EXISTS (
                    SELECT 1
                    FROM individual_individual i2
                    WHERE i2."isDeleted" = False
                      AND lower(NULLIF(btrim(i2."Json_ext"->>'external_id'), '')) =
                          lower(NULLIF(btrim(ds."Json_ext"->>'external_id'), ''))
                      AND NULLIF(btrim(ds."Json_ext"->>'external_id'), '') IS NOT NULL
              )
            RETURNING "UUID", "Json_ext"
        )
        UPDATE individual_individualdatasource AS ids
        SET individual_id = ne."UUID"
        FROM new_entry ne
        WHERE ids.upload_id = current_upload_id
          AND ids.individual_id IS NULL
          AND ids."isDeleted" = False
          AND ids."Json_ext" = ne."Json_ext"
          AND COALESCE(ids.validations ->> 'validation_errors', '[]') = '[]'
          AND (accepted IS NULL OR ids."UUID" = ANY(accepted));

        -- 2) LINK duplicates (only accepted rows) by external_id
        UPDATE individual_individualdatasource AS ids
        SET individual_id = i2."UUID"
        FROM individual_individual i2
        WHERE ids.upload_id = current_upload_id
          AND ids.individual_id IS NULL
          AND ids."isDeleted" = False
          AND COALESCE(ids.validations ->> 'validation_errors', '[]') = '[]'
          AND (accepted IS NULL OR ids."UUID" = ANY(accepted))
          AND NULLIF(btrim(ids."Json_ext"->>'external_id'), '') IS NOT NULL
          AND i2."isDeleted" = False
          AND lower(NULLIF(btrim(i2."Json_ext"->>'external_id'), '')) =
              lower(NULLIF(btrim(ids."Json_ext"->>'external_id'), ''));
EXCEPTION WHEN OTHERS THEN
    UPDATE individual_individualdatasourceupload
    SET status = 'FAIL',
        error = jsonb_build_object(
            'error', SQLERRM,
            'timestamp', NOW()::text,
            'upload_id', current_upload_id::text
        )
    WHERE "UUID" = current_upload_id;
END $$;
"""
