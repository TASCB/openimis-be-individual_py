import logging

from core.models import User
from core.utils import set_current_user, clear_current_user
from individual.workflows.utils import DataUploadWorkflow
from individual.services import IndividualImportService

logger = logging.getLogger(__name__)


def process_import_individuals_workflow(user_uuid, upload_uuid):
    # Call the records' validation service directly with the provided arguments
    user = User.objects.get(id=user_uuid)
    set_current_user(user)
    try:
        service = DataUploadWorkflow(upload_uuid, user_uuid)
        service.validate_dataframe_headers()
        service.execute(upload_sql)
        IndividualImportService(user).synchronize_data_for_reporting(upload_uuid)
    finally:
        clear_current_user()


upload_sql = """
DO $$
 DECLARE
            current_upload_id UUID := %s::UUID;
            userUUID UUID := %s::UUID;
            failing_entries UUID[];
            failing_entries_invalid_json UUID[];
            failing_entries_first_name UUID[];
            failing_entries_last_name UUID[];
            failing_entries_dob UUID[];
            BEGIN
    -- Check if all required fields are present in the entries
    SELECT ARRAY_AGG("UUID") INTO failing_entries_first_name
    FROM individual_individualdatasource
    WHERE upload_id=current_upload_id and individual_id is null and "isDeleted"=False AND NOT "Json_ext" ? 'first_name';
    SELECT ARRAY_AGG("UUID") INTO failing_entries_last_name
    FROM individual_individualdatasource
    WHERE upload_id=current_upload_id and individual_id is null and "isDeleted"=False AND NOT "Json_ext" ? 'last_name';
    SELECT ARRAY_AGG("UUID") INTO failing_entries_dob
    FROM individual_individualdatasource
    WHERE upload_id=current_upload_id and individual_id is null and "isDeleted"=False AND NOT "Json_ext" ? 'dob';  
    
    -- If any entries do not meet the criteria or missing required fields, set the error message in the upload table and do not proceed further
    IF failing_entries_invalid_json IS NOT NULL or failing_entries_first_name IS NOT NULL OR failing_entries_last_name IS NOT NULL OR failing_entries_dob IS NOT NULL THEN
        UPDATE individual_individualdatasourceupload
        SET error = coalesce(error, '{}'::jsonb) || jsonb_build_object('errors', jsonb_build_object(
                            'error', 'Invalid entries',
                            'timestamp', NOW()::text,
                            'upload_id', current_upload_id::text,
                            'failing_entries_first_name', failing_entries_first_name,
                            'failing_entries_last_name', failing_entries_last_name,
                            'failing_entries_dob', failing_entries_dob,
                            'failing_entries_invalid_json', failing_entries_invalid_json
                        ))
        WHERE "UUID" = current_upload_id;
       update individual_individualdatasourceupload set status='FAIL' where "UUID" = current_upload_id;
    -- If no invalid entries, then proceed with the data manipulation
    ELSE
        BEGIN
          WITH new_entry AS (
            INSERT INTO individual_individual(
            "UUID", "isDeleted", version, "UserCreatedUUID", "UserUpdatedUUID",
            "Json_ext", first_name, last_name, dob, location_id
            )
            SELECT gen_random_uuid(), false, 1, userUUID, userUUID,
                "Json_ext",
                "Json_ext"->>'first_name',
                "Json_ext" ->> 'last_name',
                to_date("Json_ext" ->> 'dob', 'YYYY-MM-DD'),
                loc."LocationId"
            FROM individual_individualdatasource AS ds
            LEFT JOIN "tblLocations" AS loc
                    ON loc."LocationName" = ds."Json_ext"->>'location_name'
                    AND loc."LocationCode" = ds."Json_ext"->>'location_code'
                    AND loc."LocationType"='V'
                    AND loc."ValidityTo" IS NULL
            WHERE ds.upload_id=current_upload_id 
                AND ds.individual_id is null
                AND ds."isDeleted"=False
            RETURNING "UUID", "Json_ext"  -- also return the Json_ext
          )
          UPDATE individual_individualdatasource
          SET individual_id = new_entry."UUID"
          FROM new_entry
          WHERE upload_id=current_upload_id
            and individual_id is null
            and "isDeleted"=False
            and individual_individualdatasource."Json_ext" = new_entry."Json_ext";  -- match on Json_ext
            update individual_individualdatasourceupload set status='SUCCESS', error='{}' where "UUID" = current_upload_id;
            EXCEPTION
            WHEN OTHERS then
            update individual_individualdatasourceupload set status='FAIL' where "UUID" = current_upload_id;
                UPDATE individual_individualdatasourceupload
                SET error = coalesce(error, '{}'::jsonb) || jsonb_build_object('errors', jsonb_build_object(
                                    'error', SQLERRM,
                                    'timestamp', NOW()::text,
                                    'upload_id', current_upload_id::text
                                ))
                WHERE "UUID" = current_upload_id;
        END;
    END IF;
END $$;
        """
