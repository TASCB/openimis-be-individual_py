# individual/workflows/utils.py
"""
Functionalities shared between different python workflows.
"""
import json
import logging
from abc import ABCMeta, abstractmethod
from typing import Iterable, Optional, List
from collections import OrderedDict

from django.db import ProgrammingError, connection

from core.models import User
from individual.apps import IndividualConfig
from individual.models import IndividualDataSource
from individual.services import IndividualImportService
from individual.utils import load_dataframe
from workflow.exceptions import PythonWorkflowHandlerException

logger = logging.getLogger(__name__)

# ---- Allow-list of extra headers that are NOT in the JSON schema but are valid for upload
ALLOWED_EXTRA_HEADERS = {
    # extras used by our ETL/adapter and group-linking
    "recipient_info",
    "group_code",
    "individual_role",
    "individual_role_code",
    "hhrep",
    "interview_key",
    "raw",
     "_source",
    "external_id",
    # tolerated convenience/diagnostic columns
    "json_ext",
    "pmt_score",
    "pmt_class",
    # IMPORTANT: allow a stray lowercase 'id' so accidental index/exports don't fail validation.
    # We still forbid uppercase 'ID' on IMPORT and require uppercase 'ID' on UPDATE.
    "id",
}


def _safe_parse_json_maybe(raw):
    """
    Accept either a JSON string/bytes or an already-parsed dict/OrderedDict.
    Return a dict (or {} on failure).
    """
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    if isinstance(raw, (dict, OrderedDict)):
        return dict(raw)
    return {}


def _safe_list_maybe(raw) -> List[str]:
    """
    Accept a python list/tuple/set or a JSON-encoded list (string).
    Return [] on failure.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(x) for x in raw]
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            out = json.loads(raw)
            if isinstance(out, list):
                return [str(x) for x in out]
        except Exception:
            return []
    return []


class BasePythonWorkflowExecutor(metaclass=ABCMeta):

    def __init__(self, upload_uuid, user_uuid, accepted=None):
        self.upload_uuid = upload_uuid
        self.user_uuid = user_uuid
        self.user = User.objects.get(id=self.user_uuid)
        self.accepted = accepted
        self._load_df()

    def _load_df(self):
        df = load_dataframe(IndividualDataSource.objects.filter(upload_id=self.upload_uuid))

        # ---- Normalize headers before any validation ----
        if getattr(df, "columns", None) is not None:
            # strip BOM & whitespace
            df.rename(columns=lambda c: str(c).lstrip("\ufeff").strip(), inplace=True)
            # drop common index dump columns
            cols_to_drop = [c for c in df.columns if c.startswith("Unnamed:")]
            if cols_to_drop:
                df.drop(columns=cols_to_drop, inplace=True, errors="ignore")

        self.df = self.clean_data(df)

        # Tolerate schema as JSON string or as dict (OrderedDict)
        raw_schema = getattr(IndividualConfig, "individual_schema", None)
        self.schema = _safe_parse_json_maybe(raw_schema)

        # Also normalize optional config-defined extra headers (if present)
        self._config_extra_headers = set(_safe_list_maybe(getattr(IndividualConfig, "upload_extra_headers", None)))

    @staticmethod
    def clean_data(df):
        # Backward-compat: explicitly drop the classic pandas index dump if present
        if 'Unnamed: 0' in df.columns:
            df.drop('Unnamed: 0', axis=1, inplace=True)
            logger.info("Provided dataframe contains Unnamed column for python workflow. It'll be removed from upload.")
        return df

    def validate_dataframe_headers(self, is_update=False):
        """
        Validates if DataFrame headers:
        1. Are included in the JSON schema properties OR in ALLOWED_EXTRA_HEADERS.
        2. Include core required fields (configurable) with special handling for ID:
           - Uploads (is_update=False): 'ID' MUST NOT be present; any 'id' entry in config is ignored.
           - Updates (is_update=True): require 'ID' (uppercase UUID of target Individual).
        3. 'id' (lowercase) is tolerated as an extra (often an accidental index/column).
        """
        df_headers = set(self.df.columns)

        # JSON schema properties
        schema_properties = set((_safe_parse_json_maybe(self.schema) or {}).get('properties', {}).keys())

        # Required core fields (from config), but normalize handling of id/ID
        configured_required = list(getattr(IndividualConfig, 'individual_base_fields', ['first_name', 'last_name', 'dob']))
        required_headers = {h for h in configured_required if str(h).lower() != 'id'}

        if is_update:
            # For updates, ID is required (uppercase)
            required_headers.add('ID')
        else:
            # For uploads, 'ID' must not be present in file
            if 'ID' in df_headers:
                raise PythonWorkflowHandlerException("Uploaded individuals contains invalid columns: {'ID'}")

        # Allowed extras (hard-coded + config-provided)
        allowed_extras = set(ALLOWED_EXTRA_HEADERS) | set(self._config_extra_headers)

        errors: List[str] = []

        # Validate that every non-required column is either in schema properties or in allowed extras
        unknown_headers = (df_headers - required_headers) - schema_properties - allowed_extras
        if unknown_headers:
            errors.append(f"Uploaded individuals contains invalid columns: {unknown_headers}")

        # Validate presence of required headers
        for field in required_headers:
            if field not in df_headers:
                errors.append(f"Uploaded individuals missing essential header: {field}")

        if errors:
            raise PythonWorkflowHandlerException("\n".join(errors))

    @abstractmethod
    def execute(self, **kwargs):
        pass


class SqlProcedurePythonWorkflow(BasePythonWorkflowExecutor):
    """
        Implementation of the PythonWorkflowExecutor that executes provided sql with
            current_upload_id, userUUID
        parameters.
    """

    def execute(self, sql: str, params: Iterable):
        try:
            self._execute_sql_logic(sql, params)
        except ProgrammingError as e:
            # The exception on procedure execution is handled by the procedure itself.
            logger.log(logging.WARNING, f'Error during individuals upload workflow, details:\n{str(e)}')
            return
        except Exception as e:
            raise PythonWorkflowHandlerException(str(e))

    def _execute_sql_logic(self, sql_func: str, params: Iterable):
        with connection.cursor() as cursor:
            cursor.execute(sql_func, params)


class MakerCheckerPythonWorkflowExecutor(SqlProcedurePythonWorkflow, metaclass=ABCMeta):
    """
    Implementation of the PythonWorkflowExecutor that is relying on the maker-checker logic.
    If the maker-checker logic is not applied then it's executing provided sql with
        current_upload_id, userUUID
    parameters.
    If the uploaded dataset is invalid in terms of the calculation rules validation, then new task is created.
    New task is also created in case maker-checker logic is enabled in the config.
    """
    @property
    def should_create_task(self) -> bool:
        """
        Property saying whether the maker-checker logic is enabled for given entity.
        """
        raise NotImplementedError()

    @abstractmethod
    def _create_task_function(self):
        """
        Function responsible for creating new task.
        """
        raise NotImplementedError()

    # Keep signature compatible with callers that pass (sql) only in maker-checker flows.
    def execute(self, sql, params: Optional[Iterable] = None):
        try:
            if self.should_create_task:
                # If some records were not validated, call the task creation service
                self._create_task_function()
            else:
                # All records are fine, execute SQL logic
                if params is None:
                    params = []
                self._execute_sql_logic(sql, params)
        except ProgrammingError as e:
            # The exception on procedure execution is handled by the procedure itself.
            logger.log(logging.ERROR, f'Error during individuals upload workflow, details:\n{str(e)}')
            return
        except Exception as e:
            logger.log(logging.ERROR, f'Unexpected during individuals upload workflow, details:\n{str(e)}')
            raise PythonWorkflowHandlerException(str(e))


class DataUploadWorkflow(MakerCheckerPythonWorkflowExecutor):

    def __init__(self, upload_uuid, user_uuid, import_service=IndividualImportService):
        super().__init__(upload_uuid, user_uuid)
        self.import_service = import_service(self.user)

    @property
    def should_create_task(self):
        validation_response = self.import_service.validate_import_individuals(
            upload_id=self.upload_uuid,
            individual_sources=IndividualDataSource.objects.filter(upload_id=self.upload_uuid)
        )
        # Preserve original behavior comment; if you later want to rely on config flags,
        # replace the `or True` with those flags.
        return validation_response['summary_invalid_items'] or True  # Replace this with config check

    def _create_task_function(self):
        self.import_service.create_task_with_importing_valid_items(self.upload_uuid)


class DataUpdateWorkflow(MakerCheckerPythonWorkflowExecutor):

    def __init__(self, upload_uuid, user_uuid, import_service=IndividualImportService):
        super().__init__(upload_uuid, user_uuid)
        self.import_service = import_service(self.user)

    @property
    def should_create_task(self):
        validation_response = self.import_service.validate_import_individuals(
            upload_id=self.upload_uuid,
            individual_sources=IndividualDataSource.objects.filter(upload_id=self.upload_uuid)
        )
        # Preserve original behavior comment; if you later want to rely on config flags,
        # replace the `or True` with those flags.
        return validation_response['summary_invalid_items'] or True  # Replace this with config check

    def _create_task_function(self):
        self.import_service.create_task_with_update_valid_items(self.upload_uuid)



# # individual/workflows/utils.py
# """
# Functionalities shared between different python workflows.
# """
# import json
# import logging
# from abc import ABCMeta, abstractmethod
# from typing import Iterable, Optional, List
# from collections import OrderedDict

# from django.db import ProgrammingError, connection

# from core.models import User
# from individual.apps import IndividualConfig
# from individual.models import IndividualDataSource
# from individual.services import IndividualImportService
# from individual.utils import load_dataframe
# from workflow.exceptions import PythonWorkflowHandlerException

# logger = logging.getLogger(__name__)

# # ---- Allow-list of extra headers that are NOT in the JSON schema but are valid for upload
# ALLOWED_EXTRA_HEADERS = {
#     # extras used by our ETL/adapter and group-linking
#     "recipient_info",
#     "group_code",
#     "individual_role",
#     "individual_role_code",
#     "hhrep",
#     "interview_key",
#     "external_id",
#     # important: the importer stores the whole row in Json_ext internally
#     "json_ext",
# }


# def _safe_parse_json_maybe(raw):
#     """
#     Accept either a JSON string/bytes or an already-parsed dict/OrderedDict.
#     Return a dict (or {} on failure).
#     """
#     if isinstance(raw, (str, bytes, bytearray)):
#         try:
#             return json.loads(raw)
#         except Exception:
#             return {}
#     if isinstance(raw, (dict, OrderedDict)):
#         return dict(raw)
#     return {}


# def _safe_list_maybe(raw) -> List[str]:
#     """
#     Accept a python list/tuple/set or a JSON-encoded list (string).
#     Return [] on failure.
#     """
#     if raw is None:
#         return []
#     if isinstance(raw, (list, tuple, set)):
#         return [str(x) for x in raw]
#     if isinstance(raw, (str, bytes, bytearray)):
#         try:
#             out = json.loads(raw)
#             if isinstance(out, list):
#                 return [str(x) for x in out]
#         except Exception:
#             return []
#     return []


# class BasePythonWorkflowExecutor(metaclass=ABCMeta):

#     def __init__(self, upload_uuid, user_uuid, accepted=None):
#         self.upload_uuid = upload_uuid
#         self.user_uuid = user_uuid
#         self.user = User.objects.get(id=self.user_uuid)
#         self.accepted = accepted
#         self._load_df()

#     def _load_df(self):
#         df = load_dataframe(IndividualDataSource.objects.filter(upload_id=self.upload_uuid))
#         self.df = self.clean_data(df)

#         # Tolerate schema as JSON string or as dict (OrderedDict)
#         raw_schema = getattr(IndividualConfig, "individual_schema", None)
#         self.schema = _safe_parse_json_maybe(raw_schema)

#         # Also normalize optional config-defined extra headers (if present)
#         self._config_extra_headers = set(_safe_list_maybe(getattr(IndividualConfig, "upload_extra_headers", None)))

#     @staticmethod
#     def clean_data(df):
#         if 'Unnamed: 0' in df.columns:
#             # Drop the 'Unnamed: 0' column
#             df.drop('Unnamed: 0', axis=1, inplace=True)
#             logger.info("Provided dataframe contains Unnamed column for python workflow. "
#                         "It'll be removed from upload.")
#         return df

#     def validate_dataframe_headers(self, is_update=False):
#         """
#         Validates if DataFrame headers:
#         1. Are included in the JSON schema properties OR in ALLOWED_EXTRA_HEADERS.
#         2. Include core required fields (configurable) with special handling for ID:
#            - Uploads (is_update=False): 'ID' MUST NOT be present; any 'id' requirement from config is ignored.
#            - Updates (is_update=True): require 'ID' (uppercase UUID of target Individual).
#         3. 'id' in config is treated as an UPDATE concern and not enforced for uploads.
#         """
#         df_headers = set(self.df.columns)

#         # JSON schema properties
#         schema_properties = set((_safe_parse_json_maybe(self.schema) or {}).get('properties', {}).keys())

#         # Required core fields (from config), but normalize handling of id/ID
#         configured_required = list(getattr(IndividualConfig, 'individual_base_fields', ['first_name', 'last_name', 'dob']))
#         # Normalize duplicates like "Id"/"ID"/"id"
#         configured_required_lc = [h.lower() for h in configured_required]

#         required_headers = set()
#         for idx, h in enumerate(configured_required):
#             if configured_required_lc[idx] == 'id':
#                 # never require 'id' on uploads; handled separately below for updates
#                 continue
#             required_headers.add(h)

#         if is_update:
#             # For updates, ID is required (uppercase)
#             required_headers.add('ID')
#         else:
#             # For uploads, 'ID' must not be present in file
#             if 'ID' in df_headers:
#                 raise PythonWorkflowHandlerException("Uploaded individuals contains invalid columns: {'ID'}")

#         # Allowed extras (hard-coded + config-provided)
#         allowed_extras = set(ALLOWED_EXTRA_HEADERS) | set(self._config_extra_headers)

#         errors: List[str] = []

#         # Validate that every non-required column is either in schema properties or in allowed extras
#         unknown_headers = (df_headers - required_headers) - schema_properties - allowed_extras
#         if unknown_headers:
#             errors.append(f"Uploaded individuals contains invalid columns: {unknown_headers}")

#         # Validate presence of required headers
#         for field in required_headers:
#             if field not in df_headers:
#                 errors.append(f"Uploaded individuals missing essential header: {field}")

#         if errors:
#             raise PythonWorkflowHandlerException("\n".join(errors))

#     @abstractmethod
#     def execute(self, **kwargs):
#         pass


# class SqlProcedurePythonWorkflow(BasePythonWorkflowExecutor):
#     """
#         Implementation of the PythonWorkflowExecutor that executes provided sql with
#             current_upload_id, userUUID
#         parameters.
#     """

#     def execute(self, sql: str, params: Iterable):
#         try:
#             self._execute_sql_logic(sql, params)
#         except ProgrammingError as e:
#             # The exception on procedure execution is handled by the procedure itself.
#             logger.log(logging.WARNING, f'Error during individuals upload workflow, details:\n{str(e)}')
#             return
#         except Exception as e:
#             raise PythonWorkflowHandlerException(str(e))

#     def _execute_sql_logic(self, sql_func: str, params: Iterable):
#         with connection.cursor() as cursor:
#             cursor.execute(sql_func, params)


# class MakerCheckerPythonWorkflowExecutor(SqlProcedurePythonWorkflow, metaclass=ABCMeta):
#     """
#     Implementation of the PythonWorkflowExecutor that is relying on the maker-checker logic.
#     If the maker-checker logic is not applied then it's executing provided sql with
#         current_upload_id, userUUID
#     parameters.
#     If the uploaded dataset is invalid in terms of the calculation rules validation, then new task is created.
#     New task is also created in case maker-checker logic is enabled in the config.
#     """
#     @property
#     def should_create_task(self) -> bool:
#         """
#         Property saying whether the maker-checker logic is enabled for given entity.
#         """
#         raise NotImplementedError()

#     @abstractmethod
#     def _create_task_function(self):
#         """
#         Function responsible for creating new task.
#         """
#         raise NotImplementedError()

#     # Keep signature compatible with callers that pass (sql) only in maker-checker flows.
#     def execute(self, sql, params: Optional[Iterable] = None):
#         try:
#             if self.should_create_task:
#                 # If some records were not validated, call the task creation service
#                 self._create_task_function()
#             else:
#                 # All records are fine, execute SQL logic
#                 if params is None:
#                     params = []
#                 self._execute_sql_logic(sql, params)
#         except ProgrammingError as e:
#             # The exception on procedure execution is handled by the procedure itself.
#             logger.log(logging.ERROR, f'Error during individuals upload workflow, details:\n{str(e)}')
#             return
#         except Exception as e:
#             logger.log(logging.ERROR, f'Unexpected during individuals upload workflow, details:\n{str(e)}')
#             raise PythonWorkflowHandlerException(str(e))


# class DataUploadWorkflow(MakerCheckerPythonWorkflowExecutor):

#     def __init__(self, upload_uuid, user_uuid, import_service=IndividualImportService):
#         super().__init__(upload_uuid, user_uuid)
#         self.import_service = import_service(self.user)

#     @property
#     def should_create_task(self):
#         validation_response = self.import_service.validate_import_individuals(
#             upload_id=self.upload_uuid,
#             individual_sources=IndividualDataSource.objects.filter(upload_id=self.upload_uuid)
#         )
#         # Preserve original behavior comment; if you later want to rely on config flags,
#         # replace the `or True` with those flags.
#         return validation_response['summary_invalid_items'] or True  # Replace this with config check

#     def _create_task_function(self):
#         self.import_service.create_task_with_importing_valid_items(self.upload_uuid)


# class DataUpdateWorkflow(MakerCheckerPythonWorkflowExecutor):

#     def __init__(self, upload_uuid, user_uuid, import_service=IndividualImportService):
#         super().__init__(upload_uuid, user_uuid)
#         self.import_service = import_service(self.user)

#     @property
#     def should_create_task(self):
#         validation_response = self.import_service.validate_import_individuals(
#             upload_id=self.upload_uuid,
#             individual_sources=IndividualDataSource.objects.filter(upload_id=self.upload_uuid)
#         )
#         # Preserve original behavior comment; if you later want to rely on config flags,
#         # replace the `or True` with those flags.
#         return validation_response['summary_invalid_items'] or True  # Replace this with config check

#     def _create_task_function(self):
#         self.import_service.create_task_with_update_valid_items(self.upload_uuid)


# # individual/workflows/utils.py
# """
# Functionalities shared between different python workflows.
# """
# import json
# import logging
# from abc import ABCMeta, abstractmethod
# from typing import Iterable, Optional, List
# from collections import OrderedDict

# from django.db import ProgrammingError, connection

# from core.models import User
# from individual.apps import IndividualConfig
# from individual.models import IndividualDataSource
# from individual.services import IndividualImportService
# from individual.utils import load_dataframe
# from workflow.exceptions import PythonWorkflowHandlerException

# logger = logging.getLogger(__name__)

# # ---- Allow-list of extra headers that are NOT in the JSON schema but are valid for upload
# ALLOWED_EXTRA_HEADERS = {
#     # extras used by our ETL/adapter and group-linking
#     "recipient_info",
#     "group_code",
#     "individual_role",
#     "individual_role_code",
#     "hhrep",
#     "interview_key",
#     "external_id",
#     # important: the importer stores the whole row in Json_ext internally
#     "json_ext",
# }


# def _safe_parse_json_maybe(raw):
#     """
#     Accept either a JSON string/bytes or an already-parsed dict/OrderedDict.
#     Return a dict (or {} on failure).
#     """
#     if isinstance(raw, (str, bytes, bytearray)):
#         try:
#             return json.loads(raw)
#         except Exception:
#             return {}
#     if isinstance(raw, (dict, OrderedDict)):
#         return raw
#     return {}


# def _safe_list_maybe(raw) -> List[str]:
#     """
#     Accept a python list/tuple/set or a JSON-encoded list (string).
#     Return [] on failure.
#     """
#     if raw is None:
#         return []
#     if isinstance(raw, (list, tuple, set)):
#         return [str(x) for x in raw]
#     if isinstance(raw, (str, bytes, bytearray)):
#         try:
#             out = json.loads(raw)
#             if isinstance(out, list):
#                 return [str(x) for x in out]
#         except Exception:
#             return []
#     return []


# class BasePythonWorkflowExecutor(metaclass=ABCMeta):

#     def __init__(self, upload_uuid, user_uuid, accepted=None):
#         self.upload_uuid = upload_uuid
#         self.user_uuid = user_uuid
#         self.user = User.objects.get(id=self.user_uuid)
#         self.accepted = accepted
#         self._load_df()

#     def _load_df(self):
#         df = load_dataframe(IndividualDataSource.objects.filter(upload_id=self.upload_uuid))
#         self.df = self.clean_data(df)

#         # Tolerate schema as JSON string or as dict (OrderedDict)
#         raw_schema = getattr(IndividualConfig, "individual_schema", None)
#         self.schema = _safe_parse_json_maybe(raw_schema)

#         # Also normalize optional config-defined extra headers (if present)
#         self._config_extra_headers = set(_safe_list_maybe(getattr(IndividualConfig, "upload_extra_headers", None)))

#     @staticmethod
#     def clean_data(df):
#         if 'Unnamed: 0' in df.columns:
#             # Drop the 'Unnamed: 0' column
#             df.drop('Unnamed: 0', axis=1, inplace=True)
#             logger.info("Provided dataframe contains Unnamed column for python workflow. "
#                         "It'll be removed from upload.")
#         return df

#     def validate_dataframe_headers(self, is_update=False):
#         """
#         Validates if DataFrame headers:
#         1. Are included in the JSON schema properties OR in ALLOWED_EXTRA_HEADERS.
#         2. Include 'first_name', 'last_name', and 'dob'.
#         3. 'id' is field automatically added to DataFrame which is used for upload.
#         4. If action is data update then 'ID' unique identifier is required as well.
#            (For uploads: 'ID' must NOT be present.)
#         """
#         df_headers = set(self.df.columns)

#         # JSON schema properties
#         schema_properties = set((_safe_parse_json_maybe(self.schema) or {}).get('properties', {}).keys())

#         # Required core fields (from config)
#         required_headers = set(getattr(IndividualConfig, 'individual_base_fields', ['first_name', 'last_name', 'dob']))

#         # For update, 'ID' must be present; for upload, 'ID' must not be present
#         if is_update:
#             required_headers.add('ID')

#         # Allowed extras (hard-coded + config-provided)
#         allowed_extras = set(ALLOWED_EXTRA_HEADERS) | set(self._config_extra_headers)

#         errors: List[str] = []

#         # For upload: if 'ID' shows up, that's invalid (it will be provided by UI/DB side on update flows)
#         if not is_update and 'ID' in df_headers:
#             errors.append("Uploaded individuals contains invalid columns: {'ID'}")

#         # Validate that every non-required column is either in schema properties or in allowed extras
#         # (ignore 'ID' here: handled above)
#         unknown_headers = (df_headers - required_headers) - schema_properties - allowed_extras
#         if unknown_headers:
#             errors.append(f"Uploaded individuals contains invalid columns: {unknown_headers}")

#         # Validate presence of required headers
#         for field in required_headers:
#             if field not in df_headers:
#                 errors.append(f"Uploaded individuals missing essential header: {field}")

#         if errors:
#             raise PythonWorkflowHandlerException("\n".join(errors))

#     @abstractmethod
#     def execute(self, **kwargs):
#         pass


# class SqlProcedurePythonWorkflow(BasePythonWorkflowExecutor):
#     """
#         Implementation of the PythonWorkflowExecutor that executes provided sql with
#             current_upload_id, userUUID
#         parameters.
#     """

#     def execute(self, sql: str, params: Iterable):
#         try:
#             self._execute_sql_logic(sql, params)
#         except ProgrammingError as e:
#             # The exception on procedure execution is handled by the procedure itself.
#             logger.log(logging.WARNING, f'Error during individuals upload workflow, details:\n{str(e)}')
#             return
#         except Exception as e:
#             raise PythonWorkflowHandlerException(str(e))

#     def _execute_sql_logic(self, sql_func: str, params: Iterable):
#         with connection.cursor() as cursor:
#             cursor.execute(sql_func, params)


# class MakerCheckerPythonWorkflowExecutor(SqlProcedurePythonWorkflow, metaclass=ABCMeta):
#     """
#     Implementation of the PythonWorkflowExecutor that is relying on the maker-checker logic.
#     If the maker-checker logic is not applied then it's executing provided sql with
#         current_upload_id, userUUID
#     parameters.
#     If the uploaded dataset is invalid in terms of the calculation rules validation, then new task is created.
#     New task is also created in case maker-checker logic is enabled in the config.
#     """
#     @property
#     def should_create_task(self) -> bool:
#         """
#         Property saying whether the maker-checker logic is enabled for given entity.
#         """
#         raise NotImplementedError()

#     @abstractmethod
#     def _create_task_function(self):
#         """
#         Function responsible for creating new task.
#         """
#         raise NotImplementedError()

#     # Keep signature compatible with callers that pass (sql) only in maker-checker flows.
#     def execute(self, sql, params: Optional[Iterable] = None):
#         try:
#             if self.should_create_task:
#                 # If some records were not validated, call the task creation service
#                 self._create_task_function()
#             else:
#                 # All records are fine, execute SQL logic
#                 if params is None:
#                     params = []
#                 self._execute_sql_logic(sql, params)
#         except ProgrammingError as e:
#             # The exception on procedure execution is handled by the procedure itself.
#             logger.log(logging.ERROR, f'Error during individuals upload workflow, details:\n{str(e)}')
#             return
#         except Exception as e:
#             logger.log(logging.ERROR, f'Unexpected during individuals upload workflow, details:\n{str(e)}')
#             raise PythonWorkflowHandlerException(str(e))


# class DataUploadWorkflow(MakerCheckerPythonWorkflowExecutor):

#     def __init__(self, upload_uuid, user_uuid, import_service=IndividualImportService):
#         super().__init__(upload_uuid, user_uuid)
#         self.import_service = import_service(self.user)

#     @property
#     def should_create_task(self):
#         validation_response = self.import_service.validate_import_individuals(
#             upload_id=self.upload_uuid,
#             individual_sources=IndividualDataSource.objects.filter(upload_id=self.upload_uuid)
#         )
#         # Preserve original behavior comment; if you later want to rely on config flags,
#         # replace the `or True` with those flags.
#         return validation_response['summary_invalid_items'] or True  # Replace this with config check

#     def _create_task_function(self):
#         self.import_service.create_task_with_importing_valid_items(self.upload_uuid)


# class DataUpdateWorkflow(MakerCheckerPythonWorkflowExecutor):

#     def __init__(self, upload_uuid, user_uuid, import_service=IndividualImportService):
#         super().__init__(upload_uuid, user_uuid)
#         self.import_service = import_service(self.user)

#     @property
#     def should_create_task(self):
#         validation_response = self.import_service.validate_import_individuals(
#             upload_id=self.upload_uuid,
#             individual_sources=IndividualDataSource.objects.filter(upload_id=self.upload_uuid)
#         )
#         # Preserve original behavior comment; if you later want to rely on config flags,
#         # replace the `or True` with those flags.
#         return validation_response['summary_invalid_items'] or True  # Replace this with config check

#     def _create_task_function(self):
#         self.import_service.create_task_with_update_valid_items(self.upload_uuid)



# """
# Functionalities shared between different python workflows.
# """
# import json
# import logging
# from abc import ABCMeta, abstractmethod
# from typing import Iterable, List, Optional

# from django.db import ProgrammingError, connection

# from core.models import User
# from individual.apps import IndividualConfig
# from individual.models import IndividualDataSource
# from individual.services import IndividualImportService
# from individual.utils import load_dataframe
# from workflow.exceptions import PythonWorkflowHandlerException

# # NEW: read configurable preview fields from api_etl ModuleConfiguration
# from api_etl.apps import ApiEtlConfig as C

# logger = logging.getLogger(__name__)

# # ---- Allow-list of extra headers that are NOT in the JSON schema but are valid for upload
# ALLOWED_EXTRA_HEADERS = {
#     "recipient_info",
#     "group_code",
#     "individual_role",
#     "external_id",
#     "individual_role_code",
#     "hhrep",
#     "interview_key",
#     "json_ext",
# }

# class BasePythonWorkflowExecutor(metaclass=ABCMeta):
#     def __init__(self, upload_uuid, user_uuid, accepted=None, preview_fields: Optional[List[str]] = None):
#         self.upload_uuid = upload_uuid
#         self.user_uuid = user_uuid
#         self.user = User.objects.get(id=self.user_uuid)
#         self.accepted = accepted

#         # Which columns to show in Task UI preview:
#         # 1) explicit arg if provided
#         # 2) ModuleConfiguration(api_etl).workflow_preview_fields
#         # 3) sensible default (json_ext omitted; include pmt fields so we can see them)
#         default_preview = [
#             "first_name", "last_name", "dob",
#             "location_name", "location_code",
#             "group_code", "individual_role",
#             # show PMT in preview if present (will be extracted from json_ext)
#             "pmt_score", "pmt_class",
#         ]

#         self.preview_fields: List[str] = (
#             list(preview_fields) if preview_fields
#             else list((C.config or {}).get("workflow_preview_fields", [])) or default_preview
#         )

#         self._load_df()

#     def _load_df(self):
#         df = load_dataframe(IndividualDataSource.objects.filter(upload_id=self.upload_uuid))
#         self.df = self.clean_data(df)
#         self.schema = json.loads(IndividualConfig.individual_schema)

#         # Build a separate preview dataframe without mutating self.df used for validation/import
#         self.preview_df = self._apply_preview(self.df)

#     @staticmethod
#     def clean_data(df):
#         if 'Unnamed: 0' in df.columns:
#             # Drop the 'Unnamed: 0' column
#             df.drop('Unnamed: 0', axis=1, inplace=True)
#             logger.info("Provided dataframe contains Unnamed column for python workflow. "
#                         "It'll be removed from upload.")
#         return df

#     # preview slicer with json_ext fallback extraction
#     def _apply_preview(self, df):
#         """
#         Return a dataframe limited to configured preview_fields.
#         If a preview field is missing as a flat column but present in json_ext,
#         extract it into a temporary column for the preview.
#         """
#         if df is None or getattr(df, "empty", True):
#             return df

#         out = df.copy()

#         # If json_ext column exists, normalize it (supports str or dict) for extraction
#         jx = None
#         if "json_ext" in out.columns:
#             try:
#                 # Normalize to dicts
#                 def _to_dict(x):
#                     if isinstance(x, dict):
#                         return x
#                     if isinstance(x, str) and x.strip():
#                         try:
#                             return json.loads(x)
#                         except Exception:
#                             return {}
#                     return {}

#                 jx = out["json_ext"].apply(_to_dict)
#             except Exception:
#                 jx = None

#         # Ensure columns required for preview exist; fill from json_ext if needed
#         for col in self.preview_fields:
#             if col in out.columns:
#                 continue
#             if jx is not None:
#                 try:
#                     out[col] = jx.apply(lambda d: d.get(col))
#                 except Exception:
#                     # Best-effort; if extraction fails, leave missing
#                     pass

#         # Final column selection (keep only those that now exist)
#         cols = [c for c in self.preview_fields if c in out.columns]
#         if not cols:
#             # last resort fallback so UI shows something
#             cols = [c for c in ("first_name", "last_name", "dob") if c in out.columns] or list(out.columns[:4])

#         return out[cols]

#     # helper the UI can call to get preview data easily
#     def get_preview_dataframe(self):
#         return self.preview_df

#     def validate_dataframe_headers(self, is_update=False):
#         """
#         Validates if DataFrame headers:
#         1. Are included in the JSON schema properties (plus ALLOWED_EXTRA_HEADERS).
#         2. Include 'first_name', 'last_name', and 'dob'.
#         3. 'id' is field automatically added to DataFrame which is used for upload.
#         4. If action is data upload then 'ID' unique identifier is required as well.
#         """
#         df_headers = set(self.df.columns)
#         schema_properties = set(self.schema.get('properties', {}).keys())
#         # Accept these as well even if not present in schema
#         schema_properties.update(ALLOWED_EXTRA_HEADERS)

#         required_headers = set(IndividualConfig.individual_base_fields)
#         if is_update:
#             required_headers.add('ID')

#         errors = []
#         if not (df_headers - required_headers).issubset(schema_properties):
#             invalid_headers = df_headers - schema_properties - required_headers
#             if invalid_headers:
#                 errors.append(
#                     f"Uploaded individuals contains invalid columns: {invalid_headers}"
#                 )

#         for field in required_headers:
#             if field not in df_headers:
#                 errors.append(
#                     f"Uploaded individuals missing essential header: {field}"
#                 )

#         if errors:
#             raise PythonWorkflowHandlerException("\n".join(errors))

#     @abstractmethod
#     def execute(self, **kwargs):
#         pass


# class SqlProcedurePythonWorkflow(BasePythonWorkflowExecutor):
#     """
#     Implementation of the PythonWorkflowExecutor that executes provided sql with
#     current_upload_id, userUUID parameters.
#     """

#     def execute(self, sql: str, params: Iterable):
#         try:
#             self._execute_sql_logic(sql, params)
#         except ProgrammingError as e:
#             # The exception on procedure execution is handled by the procedure itself.
#             logger.log(logging.WARNING, f'Error during individuals upload workflow, details:\n{str(e)}')
#             return
#         except Exception as e:
#             raise PythonWorkflowHandlerException(str(e))

#     def _execute_sql_logic(self, sql_func: str, params: Iterable):
#         with connection.cursor() as cursor:
#             cursor.execute(sql_func, params)
#             # Process the cursor results or handle exceptions


# class MakerCheckerPythonWorkflowExecutor(SqlProcedurePythonWorkflow, metaclass=ABCMeta):
#     """
#     Implementation of the PythonWorkflowExecutor that is relying on the maker-checker logic.
#     If the maker-checker logic is not applied then it's executing provided sql with
#     current_upload_id, userUUID parameters.
#     If the uploaded dataset is invalid in terms of the calculation rules validation, then new task is created.
#     New task is also created in case maker-checker logic is enabled in the config.
#     """
#     @property
#     def should_create_task(self) -> bool:
#         """
#         Property saying whether the maker-checker logic is enabled for given entity.
#         """
#         raise NotImplementedError()

#     @abstractmethod
#     def _create_task_function(self):
#         """
#         Function responsible for creating new task.
#         """
#         raise NotImplementedError()

#     def execute(self, sql):
#         try:
#             if self.should_create_task:
#                 # If some records were not validated, call the task creation service
#                 self._create_task_function()
#             else:
#                 # All records are fine, execute SQL logic
#                 # NOTE: pass the expected params if your SQL requires them
#                 self._execute_sql_logic(sql, params=[self.upload_uuid, self.user_uuid])
#         except ProgrammingError as e:
#             logger.log(logging.ERROR, f'Error during individuals upload workflow, details:\n{str(e)}')
#             return
#         except Exception as e:
#             logger.log(logging.ERROR, f'Unexpected during individuals upload workflow, details:\n{str(e)}')
#             raise PythonWorkflowHandlerException(str(e))


# class DataUploadWorkflow(MakerCheckerPythonWorkflowExecutor):
#     def __init__(self, upload_uuid, user_uuid, import_service=IndividualImportService):
#         # preview_fields pulled from ModuleConfiguration via Base init
#         super().__init__(upload_uuid, user_uuid)
#         self.import_service = import_service(self.user)

#     @property
#     def should_create_task(self):
#         validation_response = self.import_service.validate_import_individuals(
#             upload_id=self.upload_uuid,
#             individual_sources=IndividualDataSource.objects.filter(upload_id=self.upload_uuid)
#         )
#         # TODO: replace with a real config toggle if you want to auto-create tasks only conditionally
#         return validation_response.get('summary_invalid_items') or True

#     def _create_task_function(self):
#         self.import_service.create_task_with_importing_valid_items(self.upload_uuid)


# class DataUpdateWorkflow(MakerCheckerPythonWorkflowExecutor):
#     def __init__(self, upload_uuid, user_uuid, import_service=IndividualImportService):
#         super().__init__(upload_uuid, user_uuid)
#         self.import_service = import_service(self.user)

#     @property
#     def should_create_task(self):
#         validation_response = self.import_service.validate_import_individuals(
#             upload_id=self.upload_uuid,
#             individual_sources=IndividualDataSource.objects.filter(upload_id=self.upload_uuid)
#         )
#         return validation_response.get('summary_invalid_items') or True  # Replace with config check

#     def _create_task_function(self):
#         self.import_service.create_task_with_update_valid_items(self.upload_uuid)


# """
# Functionalities shared between different python workflows.
# """
# import json
# import logging
# from abc import ABCMeta, abstractmethod
# from typing import Iterable, List, Optional

# from django.db import ProgrammingError, connection

# from core.models import User
# from individual.apps import IndividualConfig
# from individual.models import IndividualDataSource
# from individual.services import IndividualImportService
# from individual.utils import load_dataframe
# from workflow.exceptions import PythonWorkflowHandlerException

# # NEW: read configurable preview fields from api_etl ModuleConfiguration
# from api_etl.apps import ApiEtlConfig as C

# logger = logging.getLogger(__name__)


# class BasePythonWorkflowExecutor(metaclass=ABCMeta):
#     def __init__(self, upload_uuid, user_uuid, accepted=None, preview_fields: Optional[List[str]] = None):
#         self.upload_uuid = upload_uuid
#         self.user_uuid = user_uuid
#         self.user = User.objects.get(id=self.user_uuid)
#         self.accepted = accepted

#         # Which columns to show in Task UI preview:
#         # 1) explicit arg if provided
#         # 2) ModuleConfiguration(api_etl).workflow_preview_fields
#         # 3) sensible default (note: json_ext omitted by default)
#         self.preview_fields: List[str] = (
#             list(preview_fields) if preview_fields
#             else list((C.config or {}).get("workflow_preview_fields", [])) or
#                  ["first_name", "last_name", "dob", "location_name", "location_code", "group_code", "individual_role"]
#         )

#         self._load_df()

#     def _load_df(self):
#         df = load_dataframe(IndividualDataSource.objects.filter(upload_id=self.upload_uuid))
#         self.df = self.clean_data(df)
#         self.schema = json.loads(IndividualConfig.individual_schema)

#         # Build a separate preview dataframe without mutating self.df used for validation/import
#         self.preview_df = self._apply_preview(self.df)

#     @staticmethod
#     def clean_data(df):
#         if 'Unnamed: 0' in df.columns:
#             # Drop the 'Unnamed: 0' column
#             df.drop('Unnamed: 0', axis=1, inplace=True)
#             logger.info("Provided dataframe contains Unnamed column for python workflow. "
#                         "It'll be removed from upload.")
#         return df

#     # NEW: preview slicer with json_ext fallback extraction
#     def _apply_preview(self, df):
#         """
#         Return a dataframe limited to configured preview_fields.
#         If a preview field is missing as a flat column but present in json_ext,
#         extract it into a temporary column for the preview.
#         """
#         if df is None or getattr(df, "empty", True):
#             return df

#         out = df.copy()

#         # If json_ext column exists, normalize it (supports str or dict) for extraction
#         jx = None
#         if "json_ext" in out.columns:
#             try:
#                 # Normalize to dicts
#                 def _to_dict(x):
#                     if isinstance(x, dict):
#                         return x
#                     if isinstance(x, str) and x.strip():
#                         try:
#                             return json.loads(x)
#                         except Exception:
#                             return {}
#                     return {}
#                 jx = out["json_ext"].apply(_to_dict)
#             except Exception:
#                 jx = None

#         # Ensure columns required for preview exist; fill from json_ext if needed
#         for col in self.preview_fields:
#             if col in out.columns:
#                 continue
#             if jx is not None:
#                 try:
#                     out[col] = jx.apply(lambda d: d.get(col))
#                 except Exception:
#                     # Best-effort; if extraction fails, leave missing
#                     pass

#         # Final column selection (keep only those that now exist)
#         cols = [c for c in self.preview_fields if c in out.columns]
#         if not cols:
#             # last resort fallback so UI shows something
#             cols = [c for c in ("first_name", "last_name", "dob") if c in out.columns] or list(out.columns[:4])

#         return out[cols]

#     # NEW: helper the UI can call to get preview data easily
#     def get_preview_dataframe(self):
#         return self.preview_df

#     def validate_dataframe_headers(self, is_update=False):
#         """
#         Validates if DataFrame headers:
#         1. Are included in the JSON schema properties.
#         2. Include 'first_name', 'last_name', and 'dob'.
#         3. 'id' is field automatically added to DataFrame which is used for upload.
#         4. If action is data upload then 'ID' unique identifier is required as well.
#         """
#         df_headers = set(self.df.columns)
#         schema_properties = set(self.schema.get('properties', {}).keys())
#         # Accept these as well even if not present in schema
#         schema_properties.update(['recipient_info', 'group_code', 'individual_role'])

#         required_headers = set(IndividualConfig.individual_base_fields)
#         if is_update:
#             required_headers.add('ID')

#         errors = []
#         if not (df_headers - required_headers).issubset(schema_properties):
#             invalid_headers = df_headers - schema_properties - required_headers
#             if invalid_headers:
#                 errors.append(
#                     f"Uploaded individuals contains invalid columns: {invalid_headers}"
#                 )

#         for field in required_headers:
#             if field not in df_headers:
#                 errors.append(
#                     f"Uploaded individuals missing essential header: {field}"
#                 )

#         if errors:
#             raise PythonWorkflowHandlerException("\n".join(errors))

#     @abstractmethod
#     def execute(self, **kwargs):
#         pass


# class SqlProcedurePythonWorkflow(BasePythonWorkflowExecutor):
#     """
#     Implementation of the PythonWorkflowExecutor that executes provided sql with
#     current_upload_id, userUUID parameters.
#     """

#     def execute(self, sql: str, params: Iterable):
#         try:
#             self._execute_sql_logic(sql, params)
#         except ProgrammingError as e:
#             # The exception on procedure execution is handled by the procedure itself.
#             logger.log(logging.WARNING, f'Error during individuals upload workflow, details:\n{str(e)}')
#             return
#         except Exception as e:
#             raise PythonWorkflowHandlerException(str(e))

#     def _execute_sql_logic(self, sql_func: str, params: Iterable):
#         with connection.cursor() as cursor:
#             cursor.execute(sql_func, params)
#             # Process the cursor results or handle exceptions


# class MakerCheckerPythonWorkflowExecutor(SqlProcedurePythonWorkflow, metaclass=ABCMeta):
#     """
#     Implementation of the PythonWorkflowExecutor that is relying on the maker-checker logic.
#     If the maker-checker logic is not applied then it's executing provided sql with
#     current_upload_id, userUUID parameters.
#     If the uploaded dataset is invalid in terms of the calculation rules validation, then new task is created.
#     New task is also created in case maker-checker logic is enabled in the config.
#     """
#     @property
#     def should_create_task(self) -> bool:
#         """
#         Property saying whether the maker-checker logic is enabled for given entity.
#         """
#         raise NotImplementedError()

#     @abstractmethod
#     def _create_task_function(self):
#         """
#         Function responsible for creating new task.
#         """
#         raise NotImplementedError()

#     def execute(self, sql):
#         try:
#             if self.should_create_task:
#                 # If some records were not validated, call the task creation service
#                 self._create_task_function()
#             else:
#                 # All records are fine, execute SQL logic
#                 # NOTE: pass the expected params if your SQL requires them
#                 self._execute_sql_logic(sql, params=[self.upload_uuid, self.user_uuid])
#         except ProgrammingError as e:
#             logger.log(logging.ERROR, f'Error during individuals upload workflow, details:\n{str(e)}')
#             return
#         except Exception as e:
#             logger.log(logging.ERROR, f'Unexpected during individuals upload workflow, details:\n{str(e)}')
#             raise PythonWorkflowHandlerException(str(e))


# class DataUploadWorkflow(MakerCheckerPythonWorkflowExecutor):
#     def __init__(self, upload_uuid, user_uuid, import_service=IndividualImportService):
#         # preview_fields pulled from ModuleConfiguration via Base init
#         super().__init__(upload_uuid, user_uuid)
#         self.import_service = import_service(self.user)

#     @property
#     def should_create_task(self):
#         validation_response = self.import_service.validate_import_individuals(
#             upload_id=self.upload_uuid,
#             individual_sources=IndividualDataSource.objects.filter(upload_id=self.upload_uuid)
#         )
#         # TODO: replace with a real config toggle if you want to auto-create tasks only conditionally
#         return validation_response.get('summary_invalid_items') or True

#     def _create_task_function(self):
#         self.import_service.create_task_with_importing_valid_items(self.upload_uuid)


# class DataUpdateWorkflow(MakerCheckerPythonWorkflowExecutor):
#     def __init__(self, upload_uuid, user_uuid, import_service=IndividualImportService):
#         super().__init__(upload_uuid, user_uuid)
#         self.import_service = import_service(self.user)

#     @property
#     def should_create_task(self):
#         validation_response = self.import_service.validate_import_individuals(
#             upload_id=self.upload_uuid,
#             individual_sources=IndividualDataSource.objects.filter(upload_id=self.upload_uuid)
#         )
#         return validation_response.get('summary_invalid_items') or True  # Replace with config check

#     def _create_task_function(self):
#         self.import_service.create_task_with_update_valid_items(self.upload_uuid)
