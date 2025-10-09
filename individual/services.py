# individual/services.py

import logging
import json
import uuid
import pandas as pd
import concurrent.futures
import math
import inspect
import importlib
from pandas import DataFrame
from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import transaction

from calculation.services import get_calculation_object
from core import filter_validity
from core.custom_filters import CustomFilterWizardStorage
from core.models import User
from core.services import BaseService
from core.signals import register_service_signal
from django.apps import apps
from django.utils.translation import gettext as _
from django.db.models import Q, OuterRef, Subquery, Count
from individual.apps import IndividualConfig
from individual.models import (
    Individual,
    IndividualDataSource,
    GroupIndividual,
    Group,
    IndividualDataUploadRecords,
    IndividualDataSourceUpload
)
from individual.utils import (
    load_dataframe,
    fetch_summary_of_valid_items,
    fetch_summary_of_broken_items
)
from individual.validation import (
    IndividualValidation,
    IndividualDataSourceValidation,
    GroupIndividualValidation,
    GroupValidation, CrateGroupAndMoveIndividualValidation
)
from core.services.utils import check_authentication as check_authentication, output_exception, output_result_success, \
    model_representation
from location.models import Location, LocationManager
from tasks_management.models import Task
from tasks_management.services import UpdateCheckerLogicServiceMixin, CreateCheckerLogicServiceMixin, \
    crud_business_data_builder, DeleteCheckerLogicServiceMixin
from workflow.systems.base import WorkflowHandler

logger = logging.getLogger(__name__)


# ---------- helpers to allow dotted-path/callable workflows ----------
class _SimpleRunner:
    """Wrap a function so it exposes .run(ctx)."""
    def __init__(self, fn, name="wrapped"):
        self._fn = fn
        self.name = name
    def run(self, ctx):
        sig = inspect.signature(self._fn)
        params = sig.parameters
        user_uuid = ctx.get("user_uuid")
        upload_uuid = ctx.get("upload_uuid")
        accepted = ctx.get("accepted")
        if "accepted" in params:
            return self._fn(user_uuid, upload_uuid, accepted)
        elif any(p.kind == p.VAR_KEYWORD for p in params.values()):
            return self._fn(user_uuid, upload_uuid, accepted=accepted)
        else:
            return self._fn(user_uuid, upload_uuid)

def _resolve_workflow_runner(workflow, user=None):
    """
    Return an object exposing .run(ctx) from a WorkflowHandler, dotted path, or callable.
    Fallbacks to NOOP (success) if nothing resolves.
    """
    if hasattr(workflow, "run"):
        return workflow  # already a handler
    if isinstance(workflow, str) and "." in workflow and " " not in workflow:
        mod_path, _, name = workflow.rpartition(".")
        try:
            mod = importlib.import_module(mod_path)
            obj = getattr(mod, name)
            if inspect.isclass(obj):
                # Optional user= in ctor
                try:
                    sig = inspect.signature(obj)
                    if "user" in sig.parameters:
                        return obj(user=user)
                except Exception:
                    pass
                return obj()
            if callable(obj):
                return _SimpleRunner(obj, name=workflow)
        except Exception:
            logger.exception("Failed to resolve workflow dotted path: %s", workflow)
    class _NoOp:
        def __init__(self, name="NOOP"):
            self.name = name
        def run(self, ctx):
            logger.warning("NOOP workflow used; nothing executed. Name requested: %s", workflow)
            return {"success": True, "detail": "noop"}
    return _NoOp(name=str(workflow))


class IndividualService(BaseService, UpdateCheckerLogicServiceMixin, DeleteCheckerLogicServiceMixin):
    @register_service_signal('individual_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    def create_update_task(self, obj_data):
        self._update_json_ext(obj_data)
        return super().create_update_task(obj_data)

    @register_service_signal('individual_service.update')
    def update(self, obj_data):
        self._update_json_ext(obj_data)
        return super().update(obj_data)

    @register_service_signal('individual_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)

    @register_service_signal('individual_service.undo_delete')
    @check_authentication
    def undo_delete(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_undo_delete(obj_data)
                obj_ = self.OBJECT_TYPE.objects.filter(id=obj_data['id']).first()
                obj_.is_deleted = False
                obj_.save(user=self.user)
                return {
                    "success": True,
                    "message": "Ok",
                    "detail": "Undo Delete",
                }
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="undo_delete", exception=exc)

    @register_service_signal('individual_service.select_individuals_to_benefit_plan')
    def select_individuals_to_benefit_plan(self, custom_filters, benefit_plan_id, status, user):
        individual_query = Individual.objects.filter(is_deleted=False)
        subquery = GroupIndividual.objects.filter(
            individual=OuterRef('pk')
        ).exclude(
            is_deleted=True
        ).values('individual')
        individual_query_with_filters = CustomFilterWizardStorage.build_custom_filters_queryset(
            "individual",
            "Individual",
            custom_filters,
            individual_query,
        )
        individual_query_with_filters = individual_query_with_filters.filter(~Q(pk__in=Subquery(subquery))).distinct()
        if benefit_plan_id:
            individuals_assigned_to_selected_programme = individual_query_with_filters. \
                filter(is_deleted=False, beneficiary__benefit_plan_id=benefit_plan_id)
            individuals_not_assigned_to_selected_programme = individual_query_with_filters.exclude(
                id__in=individuals_assigned_to_selected_programme.values_list('id', flat=True)
            )
            output = {
                "individuals_assigned_to_selected_programme": individuals_assigned_to_selected_programme,
                "individuals_not_assigned_to_selected_programme": individuals_not_assigned_to_selected_programme,
                "individual_query_with_filters": individual_query_with_filters,
                "benefit_plan_id": benefit_plan_id,
                "status": status,
                "user": user,
            }
            return output
        return None

    @register_service_signal('individual_service.create_accept_enrolment_task')
    def create_accept_enrolment_task(self, individual_queryset, benefit_plan_id):
        pass

    def _update_json_ext(self, obj_data):
        if not obj_data or 'json_ext' not in obj_data or 'location_id' not in obj_data:
            return

        json_ext = obj_data['json_ext']
        if not json_ext:
            return

        location_id = obj_data['location_id']
        if location_id:
            location = Location.objects.get(id=location_id)
            json_ext['location_str'] = str(location)
        else:
            json_ext['location_str'] = None

        obj_data['json_ext'] = json_ext

    OBJECT_TYPE = Individual

    def __init__(self, user, validation_class=IndividualValidation):
        super().__init__(user, validation_class)


class IndividualDataSourceService(BaseService):
    @register_service_signal('individual_data_source_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal('individual_data_source_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal('individual_data_source_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)

    OBJECT_TYPE = IndividualDataSource

    def __init__(self, user, validation_class=IndividualDataSourceValidation):
        super().__init__(user, validation_class)


class GroupService(
    BaseService,
    CreateCheckerLogicServiceMixin,
    UpdateCheckerLogicServiceMixin,
    DeleteCheckerLogicServiceMixin
):
    OBJECT_TYPE = Group

    def __init__(self, user, validation_class=GroupValidation):
        super().__init__(user, validation_class)

    @check_authentication
    @register_service_signal('group_service.create')
    def create(self, obj_data):
        try:
            with transaction.atomic():
                individuals_data = obj_data.pop('individuals_data', None)
                result = super().create(obj_data)
                group_id = result.get('data', {}).get('id')

                if not group_id:
                    return result

                if individuals_data:
                    individual_ids = [data["individual_id"] for data in individuals_data]
                    self._update_group_json_ext(group_id, individual_ids)
                    for data in individuals_data:
                        obj_data = {
                            'group_id': group_id,
                            'individual_id': data.get("individual_id"),
                            'role': data.get("role"),
                            'recipient_type': data.get("recipient_type")
                        }
                        service = GroupIndividualService(self.user)
                        service.create(obj_data)
                return result
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

    @check_authentication
    @register_service_signal('group_service.update')
    def update(self, obj_data):
        try:
            with transaction.atomic():
                individuals_data = obj_data.pop('individuals_data', None)
                result = super().update(obj_data)

                if not individuals_data:
                    return result

                group_id = obj_data['id']
                assigned_individuals_ids = \
                    GroupIndividual.objects.filter(group_id=group_id).values_list('individual_id', flat=True)

                service = GroupIndividualService(self.user)
                individual_ids = [data['individual_id'] for data in individuals_data]
                group = self._update_group_json_ext(group_id, individual_ids)

                for individual_id in assigned_individuals_ids:
                    if str(individual_id) not in individual_ids:
                        group_individual = GroupIndividual.objects.get(group_id=group_id, individual_id=individual_id)
                        service.delete({'id': group_individual.id})

                for data in individuals_data:
                    if uuid.UUID(data["individual_id"]) not in assigned_individuals_ids:
                        obj_data = {
                            'group_id': group_id,
                            'individual_id': data.get("individual_id"),
                            'role': data.get("role"),
                            'recipient_type': data.get("recipient_type")
                        }
                        service.create(obj_data)

                dict_repr = model_representation(group)
                return output_result_success(dict_representation=dict_repr)
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

    @register_service_signal('group_service.delete')
    def delete(self, obj_data):
        # if there ever was a requirement to undo group delete, remember to use members from json_ext, you will avoid
        # adding individuals that had been deleted from the group before group deletion
        with transaction.atomic():
            group_id = obj_data.get('id')
            group_individuals = GroupIndividual.objects.filter(group_id=group_id)
            for group_individual in group_individuals:
                # cant use .delete() on query since it will completely remove instances from db instead of marking
                # them as isDeleted
                group_individual.delete(user=self.user)
            return super().delete(obj_data)

    @transaction.atomic
    def _update_group_json_ext(self, group_id, individual_ids):
        # it makes sure GroupIndividual .save() won't add each individual separately to group json_ext
        # because their ids will be already there
        group = Group.objects.get(id=group_id)
        group_members = {
            str(individual.id): f"{individual.first_name} {individual.last_name}"
            for individual in Individual.objects.filter(id__in=individual_ids)
        }
        group.json_ext["members"] = group_members
        group.save(user=self.user)
        return group

    @register_service_signal('group_service.select_groups_to_benefit_plan')
    def select_groups_to_benefit_plan(self, custom_filters, benefit_plan_id, status, user):
        group_query = Group.objects.filter(is_deleted=False)
        # criteria will be based on head of the group
        group_query_with_filters = CustomFilterWizardStorage.build_custom_filters_queryset(
            "individual",
            "Group",
            custom_filters,
            group_query,
        )
        if benefit_plan_id:
            groups_assigned_to_selected_programme = group_query_with_filters. \
                filter(is_deleted=False, groupbeneficiary__benefit_plan_id=benefit_plan_id)
            groups_not_assigned_to_selected_programme = group_query_with_filters.exclude(
                id__in=groups_assigned_to_selected_programme.values_list('id', flat=True)
            )
            output = {
                "groups_assigned_to_selected_programme": groups_assigned_to_selected_programme,
                "groups_not_assigned_to_selected_programme": groups_not_assigned_to_selected_programme,
                "group_query_with_filters": group_query_with_filters,
                "benefit_plan_id": benefit_plan_id,
                "status": status,
                "user": user,
            }
            return output
        return None


class CreateGroupAndMoveIndividualService(CreateCheckerLogicServiceMixin):
    OBJECT_TYPE = Group

    def __init__(self, user, validation_class=CrateGroupAndMoveIndividualValidation):
        self.user = user
        self.validation_class = validation_class

    @check_authentication
    @register_service_signal('create_group_and_move_individual.create')
    def create(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_create_group_and_move_individual(self.user, **obj_data)
                group_individual_id = obj_data.pop('group_individual_id')
                group = GroupService(self.user).create(obj_data)
                # return group if it has errors
                if not group['data']:
                    return group
                group_individual = GroupIndividual.objects.filter(id=group_individual_id).first()
                group_id = group['data']['id']
                service = GroupIndividualService(self.user)
                service.update({
                    'group_id': group_id, "id": group_individual_id, "role": group_individual.role
                })
                group_and_individuals_message = {**group, 'detail': group_individual_id}
                return group_and_individuals_message
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

    def _business_data_serializer(self, data):
        def serialize(key, value):
            if key == 'group_individual_id':
                group_individual = GroupIndividual.objects.get(id=value)
                return f'{group_individual.individual.first_name} {group_individual.individual.last_name}'
            return value

        serialized_data = crud_business_data_builder(data, serialize)
        # TODO change to group code
        serialized_data['incoming_data']["id"] = 'NEW_GROUP'
        return serialized_data


class GroupIndividualService(BaseService, UpdateCheckerLogicServiceMixin):
    OBJECT_TYPE = GroupIndividual

    def __init__(self, user, validation_class=GroupIndividualValidation):
        super().__init__(user, validation_class)

    @register_service_signal('groupindividual_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    @check_authentication
    @register_service_signal('groupindividual_service.update')
    def update(self, obj_data):
        try:
            with transaction.atomic():
                group_individual_id = obj_data.get('id')
                incoming_group_id = obj_data.get('group_id')
                group_individual = GroupIndividual.objects.filter(id=group_individual_id, is_deleted=False).first()
                if not group_individual:
                    raise ValueError(f"no GroupIndividual found with this id {group_individual_id}")

                if str(group_individual.group.id) == str(incoming_group_id):
                    return super().update(obj_data)

                obj_data.pop('id', None)
                obj_data.pop('recipient_type', None)
                obj_data.pop('role', None)
                result = self.create(obj_data)
                self.delete({'id': group_individual_id})
                return result
        except Exception as exc:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

    @register_service_signal('groupindividual_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)

    def _business_data_serializer(self, data):
        def serialize(key, value):
            if key == 'id':
                group_individual = GroupIndividual.objects.get(id=value)
                return f'{group_individual.individual.first_name} {group_individual.individual.last_name}'
            if key == 'group_id':
                group = Group.objects.get(id=value)
                return group.code
            return value

        serialized_data = crud_business_data_builder(data, serialize)
        return serialized_data


class GroupAndGroupIndividualAlignmentService:
    """
        Service used in overridden .save() of GroupIndividual model.
    """

    def __init__(self, user):
        self.user = user

    def handle_head_change(self, group_individual_id, role, group_id):
        """
            Method used for making sure that during head change, the old one is set to default role.
        """
        if role == GroupIndividual.Role.HEAD:
            self._change_head(group_individual_id, group_id)

    def handle_primary_recipient_change(self, group_individual_id, recipient_type, group_id):
        """
            Method used for making sure that during primary recipient change, the old one is set to default role.
        """
        if recipient_type == GroupIndividual.RecipientType.PRIMARY:
            self._change_primary(group_individual_id, group_id)

    def update_json_ext_for_group(self, group):
        """
        This method ensures that json_ext of a group is up-to-date with its roles and members.
        Also (non-breaking enhancement): if a HEAD exists, mirror household PMT to the group
        as pmt_score_household / pmt_class_household.
        """
        group_individuals = GroupIndividual.objects.filter(group_id=group.id, is_deleted=False)
        head = group_individuals.filter(role=GroupIndividual.Role.HEAD).first()
        primary = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.PRIMARY).first()
        secondary = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.SECONDARY).first()

        group_members = {
            str(individual.individual.id): f"{individual.individual.first_name} {individual.individual.last_name}"
            for individual in group_individuals
        }

        head_str = f'{head.individual.first_name} {head.individual.last_name}' if head else None
        head_id = str(head.individual.id) if head else None
        head_json_ext = head.individual.json_ext if head and head.individual.json_ext else {}

        primary_str = f'{primary.individual.first_name} {primary.individual.last_name}' if primary else None
        primary_id = str(primary.individual.id) if primary else None

        secondary_str = f'{secondary.individual.first_name} {secondary.individual.last_name}' if secondary else None
        secondary_id = str(secondary.individual.id) if secondary else None

        changes_to_save = {}
        json_ext_minus_keys = {k: v for k, v in group.json_ext.items() if k not in [
            "members", "head", "head_id", "primary_recipient",
            "primary_recipient_id", "secondary_recipient", "secondary_recipient_id"
        ]}

        if json_ext_minus_keys != head_json_ext:
            all_keys = set(head_json_ext.keys()).union(json_ext_minus_keys.keys())
            for key in all_keys:
                value = head_json_ext.get(key)
                if value is None and key in group.json_ext:
                    del group.json_ext[key]
                else:
                    group.json_ext[key] = value

        # Mirror PMT specifically (household-level) without copying whole head json over and over
        try:
            p_score = None
            p_class = None
            if isinstance(head_json_ext, dict):
                p_score = head_json_ext.get("pmt_score")
                p_class = head_json_ext.get("pmt_class")
            if (group.json_ext.get("pmt_score_household") != p_score) or (group.json_ext.get("pmt_class_household") != p_class):
                group.json_ext["pmt_score_household"] = p_score
                group.json_ext["pmt_class_household"] = p_class
        except Exception:
            logger.debug("PMT mirror to group json_ext failed", exc_info=True)

        current_members = group.json_ext.get("members", {})
        additional_members = {k: v for k, v in group_members.items() if k not in current_members}
        remove_members = {k: v for k, v in current_members.items() if k not in group_members}
        updated_members = {**current_members, **additional_members}
        for member_id in remove_members:
            updated_members.pop(member_id, None)

        if current_members != updated_members:
            changes_to_save["members"] = updated_members

        if group.json_ext.get("head") != head_str:
            changes_to_save["head"] = head_str

        if group.json_ext.get("head_id") != head_id:
            changes_to_save["head_id"] = head_id

        if group.json_ext.get("primary_recipient") != primary_str:
            changes_to_save["primary_recipient"] = primary_str

        if group.json_ext.get("primary_recipient_id") != primary_id:
            changes_to_save["primary_recipient_id"] = primary_id

        if group.json_ext.get("secondary_recipient") != secondary_str:
            changes_to_save["secondary_recipient"] = secondary_str

        if group.json_ext.get("secondary_recipient_id") != secondary_id:
            changes_to_save["secondary_recipient_id"] = secondary_id

        if changes_to_save:
            group.json_ext.update(changes_to_save)
            group.save(update_fields=['json_ext'], user=self.user)

    def handle_assure_primary_recipient_in_group(self, group, recipient_type):
        """
            Making sure that group has a head.
        """
        if recipient_type == GroupIndividual.RecipientType.PRIMARY:
            return
        self._assure_primary_recipient_in_group(group)

    def ensure_location_consistent(self, group, individual, role):
        if group.location_id == individual.location_id:
            return

        if role == GroupIndividual.Role.HEAD and group.location_id is None:
            group.location_id = individual.location_id
            group.save(user=self.user)
        else:
            individual.location_id = group.location_id
            individual.save(user=self.user)

    def _assure_primary_recipient_in_group(self, group):
        group_individuals = GroupIndividual.objects.filter(group=group, is_deleted=False)
        primary_exists = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.PRIMARY).exists()
        head_exists = group_individuals.filter(role=GroupIndividual.Role.HEAD).exists()

        if primary_exists:
            return

        new_primary = group_individuals.first()

        if not new_primary:
            return

        new_primary.recipient_type = GroupIndividual.RecipientType.PRIMARY
        if not head_exists:
            new_primary.role = GroupIndividual.Role.HEAD
        new_primary.save(user=self.user)

    def _change_head(self, group_individual_id, group_id):
        heads_queryset = GroupIndividual.objects.filter(group_id=group_id, role=GroupIndividual.Role.HEAD)
        old_head = heads_queryset.exclude(id=group_individual_id).first()

        if not old_head:
            return

        old_head.role = None
        old_head.save(user=self.user)

    def _change_primary(self, group_individual_id, group_id):
        primaries_queryset = GroupIndividual.objects.filter(
            group_id=group_id, recipient_type=GroupIndividual.RecipientType.PRIMARY
        )
        old_primary = primaries_queryset.exclude(id=group_individual_id).first()

        if not old_primary:
            return

        old_primary.recipient_type = None
        old_primary.save(user=self.user)


class IndividualImportService:
    import_loaders = {
        # .csv
        'text/csv': lambda f: pd.read_csv(f),
        # .xlsx
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': lambda f: pd.read_excel(f),
        # .xls
        'application/vnd.ms-excel': lambda f: pd.read_excel(f),
        # .ods
        'application/vnd.oasis.opendocument.spreadsheet': lambda f: pd.read_excel(f),
    }

    def __init__(self, user):
        super().__init__()
        self.user = user

    @register_service_signal('individual.import_individuals')
    def import_individuals(self,
                           import_file: InMemoryUploadedFile,
                           workflow: WorkflowHandler,
                           group_aggregation_column: str):
        upload = self._save_sources(import_file)
        self._create_individual_data_upload_records(workflow, upload, group_aggregation_column)
        self._trigger_workflow(workflow, upload)
        return {'success': True, 'data': {'upload_uuid': upload.uuid}}

    @transaction.atomic
    def _save_sources(self, import_file):
        # Method separated as workflow execution must be independent of the atomic transaction.
        upload = self._create_upload_entry(import_file.name)
        dataframe = self._load_import_file(import_file)
        self._validate_dataframe(dataframe)
        self._save_data_source(dataframe, upload)
        return upload

    @transaction.atomic
    def _create_individual_data_upload_records(self, workflow, upload, group_aggregation_column):
        record = IndividualDataUploadRecords(
            data_upload=upload,
            workflow=workflow.name if hasattr(workflow, "name") else str(workflow),
            json_ext={"group_aggregation_column": group_aggregation_column}
        )
        record.save(user=self.user)

    def validate_import_individuals(self, upload_id: uuid, individual_sources):
        dataframe = load_dataframe(individual_sources)
        validated_dataframe, invalid_items = self._validate_possible_individuals(
            dataframe,
            upload_id
        )
        return {'success': True, 'data': validated_dataframe, 'summary_invalid_items': invalid_items}

    def synchronize_data_for_reporting(self, upload_id: uuid):
        if 'opensearch_reports' in apps.app_configs:
            from individual.documents import IndividualDocument

            individuals = Individual.objects.filter(individualdatasource__upload=upload_id)
            if not individuals:
                return

            IndividualDocument().update(individuals, 'index')

    @staticmethod
    def process_chunk(
        chunk,
        properties,
        unique_validations,
        loc_name_code_district_ids_from_db,
        user_allowed_loc_ids,
        duplicate_village_name_code_tuples,
    ):
        validated_dataframe = []
        check_location = 'location_name' in chunk.columns

        for _, row in chunk.iterrows():
            field_validation = {'row': row.to_dict(), 'validations': {}}
            for field, field_properties in properties.items():

                # Validation Calculation
                if "validationCalculation" in field_properties and field in row:
                    field_validation['validations'][field] = IndividualImportService._handle_validation_calculation(row, field, field_properties)

                # Uniqueness Check
                if "uniqueness" in field_properties and field in row:
                    field_validation['validations'][f'{field}_uniqueness'] = IndividualImportService._handle_uniqueness(row, field, unique_validations)

            if 'location_name' in chunk.columns:
                field_validation['validations']['location_name'] = (
                    IndividualImportService._validate_location(
                        row.location_name,
                        row.location_code,
                        loc_name_code_district_ids_from_db,
                        user_allowed_loc_ids,
                        duplicate_village_name_code_tuples,
                    )
                )

            validated_dataframe.append(field_validation)

        return validated_dataframe

    def _validate_possible_individuals(self, dataframe: DataFrame, upload_id: uuid):
        schema_dict = json.loads(IndividualConfig.individual_schema)
        properties = schema_dict.get("properties", {})

        unique_fields = [field for field, props in properties.items() if "uniqueness" in props]
        unique_validations = {}
        if unique_fields:
            unique_validations = {
                field: dataframe[field].duplicated(keep=False) 
                for field in unique_fields
            }

        check_location = 'location_name' in dataframe.columns
        if check_location:
            # Issue a single DB query instead of per row for efficiency
            loc_name_code_district_ids_from_db = self._query_location_district_ids(dataframe)
            user_allowed_loc_ids = LocationManager().get_allowed_ids(self.user)
            duplicate_village_name_code_tuples = self._query_duplicate_village_name_code()
        else:
            loc_name_code_district_ids_from_db = None
            user_allowed_loc_ids = None
            duplicate_village_name_code_tuples = None

        # TODO: Use ProcessPoolExecutor after resolving django dependency loading issue
        validated_dataframe = IndividualImportService.process_chunk(
            dataframe,
            properties,
            unique_validations,
            loc_name_code_district_ids_from_db,
            user_allowed_loc_ids,
            duplicate_village_name_code_tuples,
        )

        self.save_validation_error_in_data_source_bulk(validated_dataframe)
        invalid_items = fetch_summary_of_broken_items(upload_id)
        return validated_dataframe, invalid_items

    @staticmethod
    def _query_location_district_ids(df):
        unique_tuples = df[['location_name', 'location_code']].drop_duplicates()
        query = Q()
        for _, row in unique_tuples.iterrows():
            query |= Q(name=row['location_name'], code=row['location_code'])
        locations = Location.objects.filter(type="V", *filter_validity()).filter(query)
        return {(loc.name, loc.code): loc.parent.parent.id for loc in locations}

    @staticmethod
    def _query_duplicate_village_name_code():
        return (
            Location.objects
            .filter(type="V", *filter_validity())
            .values('name', 'code')
            .annotate(name_count=Count('id'))
            .filter(name_count__gt=1)
            .values_list('name', 'code')
        )

    @staticmethod
    def _validate_location(
        location_name,
        location_code,
        loc_name_code_district_ids_from_db,
        user_allowed_loc_ids,
        duplicate_village_name_code_tuples
    ):
        result = {
            'field_name': 'location_name',
        }
        if (pd.isna(location_name) or location_name == "") and (pd.isna(location_code) or location_code == ""):
            result['success'] = True
        elif loc_name_code_district_ids_from_db is None and user_allowed_loc_ids is None:
            result['success'] = True
        elif (location_name, location_code) not in loc_name_code_district_ids_from_db:
            result['success'] = False
            result['note'] = f"Location with name '{location_name}' and code '{location_code}' is not valid. Please check the spelling against the list of locations in the system."
        elif (location_name, location_code) in duplicate_village_name_code_tuples:
            result['success'] = False
            result['note'] = f"Location with name '{location_name}' and code '{location_code}' is ambiguous, because there are more than one location with this name and code found in the system."
        elif loc_name_code_district_ids_from_db[(location_name, location_code)] not in user_allowed_loc_ids:
            result['success'] = False
            result['note'] = f"Location with name '{location_name}' and code '{location_code}' is outside the current user's location permissions."
        else:
            result['success'] = True
        return result

    @staticmethod
    def _handle_uniqueness(row, field, unique_validations):
        success = not unique_validations[field].loc[row.name]
        result = {
            "success": success,
            "field_name": field,
        }
        if not success:
            result["note"] = f"'{field}' Field value '{row[field]}' is duplicated"
        return result

    @staticmethod
    def _handle_validation_calculation(row, field, field_properties):
        validation_calculation = field_properties.get("validationCalculation", {}).get("name")
        if not validation_calculation:
            raise ValueError("Missing validation name")
        calculation_uuid = IndividualConfig.validation_calculation_uuid
        calculation = get_calculation_object(calculation_uuid)
        result_row = calculation.calculate_if_active_for_object(
            validation_calculation,
            calculation_uuid,
            field_name=field,
            field_value=row[field],
        )
        return result_row

    def _create_upload_entry(self, filename):
        upload = IndividualDataSourceUpload(source_name=filename, source_type='individual import')
        upload.save(username=self.user.login_name)
        return upload

    def _validate_dataframe(self, dataframe: pd.DataFrame):
        if dataframe is None:
            raise ValueError("Unknown error while loading import file")
        if dataframe.empty:
            raise ValueError("Import file is empty")

    def _load_import_file(self, import_file) -> pd.DataFrame:
        if import_file.content_type not in self.import_loaders:
            raise ValueError("Unsupported content type: {}".format(import_file.content_type))
        return self.import_loaders[import_file.content_type](import_file)

    def _save_data_source(self, dataframe: pd.DataFrame, upload: IndividualDataSourceUpload):
        data_source_objects = []
        for _, row in dataframe.iterrows():
            ds = IndividualDataSource(
                upload=upload,
                json_ext=json.loads(row.to_json()),
                validations={},
                user_created=self.user,
                user_updated=self.user,
                uuid=uuid.uuid4()
            )
            data_source_objects.append(ds)
        IndividualDataSource.objects.bulk_create(data_source_objects)

    def _trigger_workflow(self,
                          workflow: WorkflowHandler,
                          upload: IndividualDataSourceUpload):
        """
        Trigger the configured workflow for this upload, with proper status transitions
        and error capture (matches original behavior). Also supports dotted-path/callables.
        """
        #if no workflow 
        if workflow is None:
            raise ValueError("No workflow provided for import_individuals")
            
        try:
            # Before the run in order to avoid racing conditions
            upload.status = IndividualDataSourceUpload.Status.TRIGGERED
            upload.save(username=self.user.login_name)
            
            # Resolve to an object exposing .run(ctx)
            runner = _resolve_workflow_runner(workflow, user=self.user)

            # Core user UUID required
            core_user = User.objects.get(username=self.user.login_name)
            user_uuid = str(getattr(core_user, "id"))

            result = runner.run({
                'user_uuid': user_uuid,
                'upload_uuid': str(upload.uuid),
            })

            # Structured failure from handler -> mark FAIL and record error
            if result and isinstance(result, dict) and result.get('success') is False:
                raise ValueError(result.get('message', 'Unexpected error during the workflow execution'))

        except ValueError as e:
            upload.status = IndividualDataSourceUpload.Status.FAIL
            upload.error = {'workflow': str(e)}
            upload.save(username=self.user.login_name)
            return upload
        except Exception as e:
            upload.status = IndividualDataSourceUpload.Status.FAIL
            upload.error = {'workflow': str(e)}
            upload.save(username=self.user.login_name)
            logger.exception("Workflow crashed for upload %s", upload.uuid)
            return upload

    def link_groups_for_upload_uuid(self, upload_uuid: str) -> dict:
        """
        Create/align Groups and GroupIndividuals for all Individuals created by this upload.
        - Sets HEAD when individual_role_code == '1'
        - Sets PRIMARY when hhrep == individual_role_code
        - Aligns locations (group vs individual) before linking
        - Copies HEAD's PMT (if present) to Group.json_ext as pmt_score_household / pmt_class_household
        - Refreshes group.json_ext members/head/recipients
        """
        from individual.models import (
            Individual, Group, GroupIndividual,
            IndividualDataUploadRecords, IndividualDataSourceUpload
        )
        from individual.services import (
            GroupIndividualService, GroupAndGroupIndividualAlignmentService
        )

        upload = IndividualDataSourceUpload.objects.filter(uuid=upload_uuid, is_deleted=False).first()
        if not upload:
            return {"success": False, "message": f"Upload {upload_uuid} not found"}

        # Determine grouping column (defaults to 'group_code')
        group_col = "group_code"
        rec = (IndividualDataUploadRecords.objects
               .filter(data_upload=upload, is_deleted=False)
               .order_by("id").first())
        if rec and isinstance(rec.json_ext, dict):
            c = (rec.json_ext or {}).get("group_aggregation_column")
            if isinstance(c, str) and c.strip():
                group_col = c.strip()

        inds = (Individual.objects
                .filter(individualdatasource__upload=upload, is_deleted=False)
                .distinct())

        if not inds.exists():
            return {"success": True, "group_column": group_col, "created_groups": 0,
                    "created_links": 0, "updated_links": 0, "groups_touched": 0}

        aligner = GroupAndGroupIndividualAlignmentService(self.user)

        created_groups = 0
        created_links = 0
        updated_links = 0
        touched_groups = set()

        with transaction.atomic():
            for ind in inds:
                # Resolve group code from top-level or json_ext fallback
                group_code = getattr(ind, group_col, None)
                if not group_code:
                    jx = ind.json_ext or {}
                    group_code = jx.get(group_col) or jx.get("group_code")
                if not group_code:
                    continue

                grp = Group.objects.filter(code=group_code, is_deleted=False).first()
                if not grp:
                    grp = Group(code=group_code, json_ext={})
                    grp.save(user=self.user)
                    created_groups += 1
                touched_groups.add(grp.id)

                # Role / recipient inference from individual's json_ext
                jx = ind.json_ext or {}
                role_code = str(jx.get("individual_role_code") or jx.get("relationship_to_head") or "").strip()
                hhrep_code = str(jx.get("hhrep") or "").strip()

                desired_role = GroupIndividual.Role.HEAD if role_code == "1" else None
                desired_recipient = (GroupIndividual.RecipientType.PRIMARY
                                     if (hhrep_code and hhrep_code == role_code) else None)

                # Align locations BEFORE linking (mirrors your earlier logic)
                has_head = GroupIndividual.objects.filter(
                    group=grp, role=GroupIndividual.Role.HEAD, is_deleted=False
                ).exists()
                role_for_alignment = desired_role if (desired_role == GroupIndividual.Role.HEAD or not has_head) else None
                try:
                    aligner.ensure_location_consistent(grp, ind, role_for_alignment)
                except Exception:
                    # non-fatal alignment error
                    pass

                gi = GroupIndividual.objects.filter(group=grp, individual=ind, is_deleted=False).first()
                if not gi:
                    GroupIndividualService(self.user).create({
                        "group_id": str(grp.id),
                        "individual_id": str(ind.id),
                        "role": desired_role,
                        "recipient_type": desired_recipient,
                    })
                    created_links += 1
                else:
                    changed = False
                    if gi.role != desired_role:
                        gi.role = desired_role
                        changed = True
                    if gi.recipient_type != desired_recipient:
                        gi.recipient_type = desired_recipient
                        changed = True
                    if changed:
                        gi.save(user=self.user)
                        updated_links += 1

                # Copy PMT from HEAD to group json_ext (optional but useful)
                try:
                    if desired_role == GroupIndividual.Role.HEAD:
                        pmt_score = jx.get("pmt_score")
                        pmt_class = jx.get("pmt_class")
                        if pmt_score is not None or pmt_class is not None:
                            upd = False
                            if grp.json_ext is None:
                                grp.json_ext = {}
                            if pmt_score is not None and grp.json_ext.get("pmt_score_household") != pmt_score:
                                grp.json_ext["pmt_score_household"] = pmt_score
                                upd = True
                            if pmt_class is not None and grp.json_ext.get("pmt_class_household") != pmt_class:
                                grp.json_ext["pmt_class_household"] = pmt_class
                                upd = True
                            if upd:
                                grp.save(update_fields=["json_ext"], user=self.user)
                except Exception:
                    pass

            # Refresh group.json_ext aggregates once per touched group
            for gid in touched_groups:
                try:
                    g = Group.objects.get(id=gid)
                    aligner.update_json_ext_for_group(g)
                except Exception:
                    pass

        return {
            "success": True,
            "group_column": group_col,
            "created_groups": created_groups,
            "created_links": created_links,
            "updated_links": updated_links,
            "groups_touched": len(touched_groups),
        }

    def link_groups_for_upload_id(self, upload_id) -> dict:
        """Helper: accept DB PK and forward to UUID-based method."""
        from individual.models import IndividualDataSourceUpload
        up = IndividualDataSourceUpload.objects.filter(id=upload_id, is_deleted=False).first()
        if not up:
            return {"success": False, "message": f"Upload id {upload_id} not found"}
        return self.link_groups_for_upload_uuid(str(up.uuid))

    # Backward-compatible alias if any caller expects this name
    def finalize_upload_group_links(self, upload_uuid: str) -> dict:
        return self.link_groups_for_upload_uuid(upload_uuid)

    def save_validation_error_in_data_source_bulk(self, validated_dataframe):
        data_sources_to_update = []

        for field_validation in validated_dataframe:
            row = field_validation['row']
            error_fields = []

            for key, value in field_validation['validations'].items():
                if not value.get('success', False):
                    error_fields.append({
                        "field_name": value.get('field_name'),
                        "note": value.get('note')
                    })

            data_sources_to_update.append(
                IndividualDataSource(
                    id=row['id'],
                    validations={'validation_errors': error_fields}
                )
            )

        if data_sources_to_update:
            IndividualDataSource.objects.bulk_update(data_sources_to_update, ['validations'])

    def create_task_with_importing_valid_items(self, upload_id: uuid):
        if IndividualConfig.enable_maker_checker_for_individual_upload:
            IndividualTaskCreatorService(self.user) \
                .create_task_with_importing_valid_items(upload_id)
        else:
            record = IndividualDataUploadRecords.objects.get(
                data_upload_id=upload_id,
                is_deleted=False
            )
            from individual.signals.on_validation_import_valid_items import IndividualItemsImportTaskCompletionEvent
            IndividualItemsImportTaskCompletionEvent(
                IndividualConfig.validation_import_valid_items_workflow,
                record,
                record.data_upload.id,
                self.user
            ).run_workflow()

    def create_task_with_update_valid_items(self, upload_id: uuid):
        # Resolve automatically if maker-checker not enabled
        if IndividualConfig.enable_maker_checker_for_individual_update:
            IndividualTaskCreatorService(self.user) \
                .create_task_with_update_valid_items(upload_id)
        else:
            record = IndividualDataUploadRecords.objects.get(
                data_upload_id=upload_id,
                is_deleted=False
            )
            from individual.signals.on_validation_import_valid_items import IndividualItemsUploadTaskCompletionEvent
            IndividualItemsUploadTaskCompletionEvent(
                IndividualConfig.validation_upload_valid_items_workflow,
                record,
                record.data_upload.id,
                self.user
            ).run_workflow()


class IndividualTaskCreatorService:

    def __init__(self, user):
        self.user = user

    def create_task_with_importing_valid_items(self, upload_id: uuid):
        self._create_task(upload_id, IndividualConfig.validation_import_valid_items)

    def create_task_with_update_valid_items(self, upload_id: uuid):
        self._create_task(upload_id, IndividualConfig.validation_upload_valid_items)

    @register_service_signal('individual.update_task')
    @transaction.atomic()
    def _create_task(self, upload_id, business_event):
        from tasks_management.services import TaskService
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.models import Task
        upload_record = IndividualDataUploadRecords.objects.get(
            data_upload_id=upload_id,
            is_deleted=False
        )
        json_ext = {
            'source_name': upload_record.data_upload.source_name,
            'workflow': upload_record.workflow,
            'percentage_of_invalid_items': self.__calculate_percentage_of_invalid_items(upload_id),
            'data_upload_id': str(upload_id),
            'group_aggregation_column':
                upload_record.json_ext.get('group_aggregation_column')
                if isinstance(upload_record.json_ext, dict)
                else None,
        }
        TaskService(self.user).create({
            'source': 'import_valid_items',
            'entity': upload_record,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': business_event,
            'json_ext': json_ext
        })

        data_upload = upload_record.data_upload
        data_upload.status = IndividualDataSourceUpload.Status.WAITING_FOR_VERIFICATION
        data_upload.save(user=self.user)

    def __calculate_percentage_of_invalid_items(self, upload_id):
        number_of_valid_items = len(fetch_summary_of_valid_items(upload_id))
        number_of_invalid_items = len(fetch_summary_of_broken_items(upload_id))
        total_items = number_of_invalid_items + number_of_valid_items

        if total_items == 0:
            percentage_of_invalid_items = 0
        else:
            percentage_of_invalid_items = (number_of_invalid_items / total_items) * 100

        percentage_of_invalid_items = round(percentage_of_invalid_items, 2)
        return percentage_of_invalid_items



# import logging
# import json
# import uuid
# import pandas as pd
# import concurrent.futures
# import math
# from pandas import DataFrame
# from django.core.files.uploadedfile import InMemoryUploadedFile
# from django.db import transaction

# from calculation.services import get_calculation_object
# from core import filter_validity
# from core.custom_filters import CustomFilterWizardStorage
# from core.models import User
# from core.services import BaseService
# from core.signals import register_service_signal
# from django.apps import apps
# from django.utils.translation import gettext as _
# from django.db.models import Q, OuterRef, Subquery, Count
# from individual.apps import IndividualConfig
# from individual.models import (
#     Individual,
#     IndividualDataSource,
#     GroupIndividual,
#     Group,
#     IndividualDataUploadRecords,
#     IndividualDataSourceUpload
# )
# from individual.utils import (
#     load_dataframe,
#     fetch_summary_of_valid_items,
#     fetch_summary_of_broken_items
# )
# from individual.validation import (
#     IndividualValidation,
#     IndividualDataSourceValidation,
#     GroupIndividualValidation,
#     GroupValidation, CrateGroupAndMoveIndividualValidation
# )
# from core.services.utils import check_authentication as check_authentication, output_exception, output_result_success, \
#     model_representation
# from location.models import Location, LocationManager
# from tasks_management.models import Task
# from tasks_management.services import UpdateCheckerLogicServiceMixin, CreateCheckerLogicServiceMixin, \
#     crud_business_data_builder, DeleteCheckerLogicServiceMixin
# from workflow.systems.base import WorkflowHandler

# logger = logging.getLogger(__name__)


# class IndividualService(BaseService, UpdateCheckerLogicServiceMixin, DeleteCheckerLogicServiceMixin):
#     @register_service_signal('individual_service.create')
#     def create(self, obj_data):
#         return super().create(obj_data)

#     def create_update_task(self, obj_data):
#         self._update_json_ext(obj_data)
#         return super().create_update_task(obj_data)

#     @register_service_signal('individual_service.update')
#     def update(self, obj_data):
#         self._update_json_ext(obj_data)
#         return super().update(obj_data)

#     @register_service_signal('individual_service.delete')
#     def delete(self, obj_data):
#         return super().delete(obj_data)

#     @register_service_signal('individual_service.undo_delete')
#     @check_authentication
#     def undo_delete(self, obj_data):
#         try:
#             with transaction.atomic():
#                 self.validation_class.validate_undo_delete(obj_data)
#                 obj_ = self.OBJECT_TYPE.objects.filter(id=obj_data['id']).first()
#                 obj_.is_deleted = False
#                 obj_.save(user=self.user)
#                 return {
#                     "success": True,
#                     "message": "Ok",
#                     "detail": "Undo Delete",
#                 }
#         except Exception as exc:
#             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="undo_delete", exception=exc)

#     @register_service_signal('individual_service.select_individuals_to_benefit_plan')
#     def select_individuals_to_benefit_plan(self, custom_filters, benefit_plan_id, status, user):
#         individual_query = Individual.objects.filter(is_deleted=False)
#         subquery = GroupIndividual.objects.filter(
#             individual=OuterRef('pk')
#         ).exclude(
#             is_deleted=True
#         ).values('individual')
#         individual_query_with_filters = CustomFilterWizardStorage.build_custom_filters_queryset(
#             "individual",
#             "Individual",
#             custom_filters,
#             individual_query,
#         )
#         individual_query_with_filters = individual_query_with_filters.filter(~Q(pk__in=Subquery(subquery))).distinct()
#         if benefit_plan_id:
#             individuals_assigned_to_selected_programme = individual_query_with_filters. \
#                 filter(is_deleted=False, beneficiary__benefit_plan_id=benefit_plan_id)
#             individuals_not_assigned_to_selected_programme = individual_query_with_filters.exclude(
#                 id__in=individuals_assigned_to_selected_programme.values_list('id', flat=True)
#             )
#             output = {
#                 "individuals_assigned_to_selected_programme": individuals_assigned_to_selected_programme,
#                 "individuals_not_assigned_to_selected_programme": individuals_not_assigned_to_selected_programme,
#                 "individual_query_with_filters": individual_query_with_filters,
#                 "benefit_plan_id": benefit_plan_id,
#                 "status": status,
#                 "user": user,
#             }
#             return output
#         return None

#     @register_service_signal('individual_service.create_accept_enrolment_task')
#     def create_accept_enrolment_task(self, individual_queryset, benefit_plan_id):
#         pass

#     def _update_json_ext(self, obj_data):
#         if not obj_data or 'json_ext' not in obj_data or 'location_id' not in obj_data:
#             return

#         json_ext = obj_data['json_ext']
#         if not json_ext:
#             return

#         location_id = obj_data['location_id']
#         if location_id:
#             location = Location.objects.get(id=location_id)
#             json_ext['location_str'] = str(location)
#         else:
#             json_ext['location_str'] = None

#         obj_data['json_ext'] = json_ext

#     OBJECT_TYPE = Individual

#     def __init__(self, user, validation_class=IndividualValidation):
#         super().__init__(user, validation_class)


# class IndividualDataSourceService(BaseService):
#     @register_service_signal('individual_data_source_service.create')
#     def create(self, obj_data):
#         return super().create(obj_data)

#     @register_service_signal('individual_data_source_service.update')
#     def update(self, obj_data):
#         return super().update(obj_data)

#     @register_service_signal('individual_data_source_service.delete')
#     def delete(self, obj_data):
#         return super().delete(obj_data)

#     OBJECT_TYPE = IndividualDataSource

#     def __init__(self, user, validation_class=IndividualDataSourceValidation):
#         super().__init__(user, validation_class)


# class GroupService(
#     BaseService,
#     CreateCheckerLogicServiceMixin,
#     UpdateCheckerLogicServiceMixin,
#     DeleteCheckerLogicServiceMixin
# ):
#     OBJECT_TYPE = Group

#     def __init__(self, user, validation_class=GroupValidation):
#         super().__init__(user, validation_class)

#     @check_authentication
#     @register_service_signal('group_service.create')
#     def create(self, obj_data):
#         try:
#             with transaction.atomic():
#                 individuals_data = obj_data.pop('individuals_data', None)
#                 result = super().create(obj_data)
#                 group_id = result.get('data', {}).get('id')

#                 if not group_id:
#                     return result

#                 if individuals_data:
#                     individual_ids = [data["individual_id"] for data in individuals_data]
#                     self._update_group_json_ext(group_id, individual_ids)
#                     for data in individuals_data:
#                         obj_data = {
#                             'group_id': group_id,
#                             'individual_id': data.get("individual_id"),
#                             'role': data.get("role"),
#                             'recipient_type': data.get("recipient_type")
#                         }
#                         service = GroupIndividualService(self.user)
#                         service.create(obj_data)
#                 return result
#         except Exception as exc:
#             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

#     @check_authentication
#     @register_service_signal('group_service.update')
#     def update(self, obj_data):
#         try:
#             with transaction.atomic():
#                 individuals_data = obj_data.pop('individuals_data', None)
#                 result = super().update(obj_data)

#                 if not individuals_data:
#                     return result

#                 group_id = obj_data['id']
#                 assigned_individuals_ids = \
#                     GroupIndividual.objects.filter(group_id=group_id).values_list('individual_id', flat=True)

#                 service = GroupIndividualService(self.user)
#                 individual_ids = [data['individual_id'] for data in individuals_data]
#                 group = self._update_group_json_ext(group_id, individual_ids)

#                 for individual_id in assigned_individuals_ids:
#                     if str(individual_id) not in individual_ids:
#                         group_individual = GroupIndividual.objects.get(group_id=group_id, individual_id=individual_id)
#                         service.delete({'id': group_individual.id})

#                 for data in individuals_data:
#                     if uuid.UUID(data["individual_id"]) not in assigned_individuals_ids:
#                         obj_data = {
#                             'group_id': group_id,
#                             'individual_id': data.get("individual_id"),
#                             'role': data.get("role"),
#                             'recipient_type': data.get("recipient_type")
#                         }
#                         service.create(obj_data)

#                 dict_repr = model_representation(group)
#                 return output_result_success(dict_representation=dict_repr)
#         except Exception as exc:
#             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

#     @register_service_signal('group_service.delete')
#     def delete(self, obj_data):
#         # if there ever was a requirement to undo group delete, remember to use members from json_ext, you will avoid
#         # adding individuals that had been deleted from the group before group deletion
#         with transaction.atomic():
#             group_id = obj_data.get('id')
#             group_individuals = GroupIndividual.objects.filter(group_id=group_id)
#             for group_individual in group_individuals:
#                 # cant use .delete() on query since it will completely remove instances from db instead of marking
#                 # them as isDeleted
#                 group_individual.delete(user=self.user)
#             return super().delete(obj_data)

#     @transaction.atomic
#     def _update_group_json_ext(self, group_id, individual_ids):
#         # it makes sure GroupIndividual .save() won't add each individual separately to group json_ext
#         # because their ids will be already there
#         group = Group.objects.get(id=group_id)
#         group_members = {
#             str(individual.id): f"{individual.first_name} {individual.last_name}"
#             for individual in Individual.objects.filter(id__in=individual_ids)
#         }
#         group.json_ext["members"] = group_members
#         group.save(user=self.user)
#         return group

#     @register_service_signal('group_service.select_groups_to_benefit_plan')
#     def select_groups_to_benefit_plan(self, custom_filters, benefit_plan_id, status, user):
#         group_query = Group.objects.filter(is_deleted=False)
#         # criteria will be based on head of the group
#         group_query_with_filters = CustomFilterWizardStorage.build_custom_filters_queryset(
#             "individual",
#             "Group",
#             custom_filters,
#             group_query,
#         )
#         if benefit_plan_id:
#             groups_assigned_to_selected_programme = group_query_with_filters. \
#                 filter(is_deleted=False, groupbeneficiary__benefit_plan_id=benefit_plan_id)
#             groups_not_assigned_to_selected_programme = group_query_with_filters.exclude(
#                 id__in=groups_assigned_to_selected_programme.values_list('id', flat=True)
#             )
#             output = {
#                 "groups_assigned_to_selected_programme": groups_assigned_to_selected_programme,
#                 "groups_not_assigned_to_selected_programme": groups_not_assigned_to_selected_programme,
#                 "group_query_with_filters": group_query_with_filters,
#                 "benefit_plan_id": benefit_plan_id,
#                 "status": status,
#                 "user": user,
#             }
#             return output
#         return None


# class CreateGroupAndMoveIndividualService(CreateCheckerLogicServiceMixin):
#     OBJECT_TYPE = Group

#     def __init__(self, user, validation_class=CrateGroupAndMoveIndividualValidation):
#         self.user = user
#         self.validation_class = validation_class

#     @check_authentication
#     @register_service_signal('create_group_and_move_individual.create')
#     def create(self, obj_data):
#         try:
#             with transaction.atomic():
#                 self.validation_class.validate_create_group_and_move_individual(self.user, **obj_data)
#                 group_individual_id = obj_data.pop('group_individual_id')
#                 group = GroupService(self.user).create(obj_data)
#                 # return group if it has errors
#                 if not group['data']:
#                     return group
#                 group_individual = GroupIndividual.objects.filter(id=group_individual_id).first()
#                 group_id = group['data']['id']
#                 service = GroupIndividualService(self.user)
#                 service.update({
#                     'group_id': group_id, "id": group_individual_id, "role": group_individual.role
#                 })
#                 group_and_individuals_message = {**group, 'detail': group_individual_id}
#                 return group_and_individuals_message
#         except Exception as exc:
#             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

#     def _business_data_serializer(self, data):
#         def serialize(key, value):
#             if key == 'group_individual_id':
#                 group_individual = GroupIndividual.objects.get(id=value)
#                 return f'{group_individual.individual.first_name} {group_individual.individual.last_name}'
#             return value

#         serialized_data = crud_business_data_builder(data, serialize)
#         # TODO change to group code
#         serialized_data['incoming_data']["id"] = 'NEW_GROUP'
#         return serialized_data


# class GroupIndividualService(BaseService, UpdateCheckerLogicServiceMixin):
#     OBJECT_TYPE = GroupIndividual

#     def __init__(self, user, validation_class=GroupIndividualValidation):
#         super().__init__(user, validation_class)

#     @register_service_signal('groupindividual_service.create')
#     def create(self, obj_data):
#         return super().create(obj_data)

#     @check_authentication
#     @register_service_signal('groupindividual_service.update')
#     def update(self, obj_data):
#         try:
#             with transaction.atomic():
#                 group_individual_id = obj_data.get('id')
#                 incoming_group_id = obj_data.get('group_id')
#                 group_individual = GroupIndividual.objects.filter(id=group_individual_id, is_deleted=False).first()
#                 if not group_individual:
#                     raise ValueError(f"no GroupIndividual found with this id {group_individual_id}")

#                 if str(group_individual.group.id) == str(incoming_group_id):
#                     return super().update(obj_data)

#                 obj_data.pop('id', None)
#                 obj_data.pop('recipient_type', None)
#                 obj_data.pop('role', None)
#                 result = self.create(obj_data)
#                 self.delete({'id': group_individual_id})
#                 return result
#         except Exception as exc:
#             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

#     @register_service_signal('groupindividual_service.delete')
#     def delete(self, obj_data):
#         return super().delete(obj_data)

#     def _business_data_serializer(self, data):
#         def serialize(key, value):
#             if key == 'id':
#                 group_individual = GroupIndividual.objects.get(id=value)
#                 return f'{group_individual.individual.first_name} {group_individual.individual.last_name}'
#             if key == 'group_id':
#                 group = Group.objects.get(id=value)
#                 return group.code
#             return value

#         serialized_data = crud_business_data_builder(data, serialize)
#         return serialized_data


# class GroupAndGroupIndividualAlignmentService:
#     """
#         Service used in overridden .save() of GroupIndividual model.
#     """

#     def __init__(self, user):
#         self.user = user

#     def handle_head_change(self, group_individual_id, role, group_id):
#         """
#             Method used for making sure that during head change, the old one is set to default role.
#         """
#         if role == GroupIndividual.Role.HEAD:
#             self._change_head(group_individual_id, group_id)

#     def handle_primary_recipient_change(self, group_individual_id, recipient_type, group_id):
#         """
#             Method used for making sure that during primary recipient change, the old one is set to default role.
#         """
#         if recipient_type == GroupIndividual.RecipientType.PRIMARY:
#             self._change_primary(group_individual_id, group_id)

#     def update_json_ext_for_group(self, group):
#         """
#         This method ensures that json_ext of a group is up-to-date with its roles and members.
#         """
#         group_individuals = GroupIndividual.objects.filter(group_id=group.id, is_deleted=False)
#         head = group_individuals.filter(role=GroupIndividual.Role.HEAD).first()
#         primary = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.PRIMARY).first()
#         secondary = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.SECONDARY).first()

#         group_members = {
#             str(individual.individual.id): f"{individual.individual.first_name} {individual.individual.last_name}"
#             for individual in group_individuals
#         }

#         head_str = f'{head.individual.first_name} {head.individual.last_name}' if head else None
#         head_id = str(head.individual.id) if head else None
#         head_json_ext = head.individual.json_ext if head and head.individual.json_ext else {}

#         primary_str = f'{primary.individual.first_name} {primary.individual.last_name}' if primary else None
#         primary_id = str(primary.individual.id) if primary else None

#         secondary_str = f'{secondary.individual.first_name} {secondary.individual.last_name}' if secondary else None
#         secondary_id = str(secondary.individual.id) if secondary else None

#         changes_to_save = {}
#         json_ext_minus_keys = {k: v for k, v in group.json_ext.items() if k not in [
#             "members", "head", "head_id", "primary_recipient",
#             "primary_recipient_id", "secondary_recipient", "secondary_recipient_id"
#         ]}

#         if json_ext_minus_keys != head_json_ext:
#             all_keys = set(head_json_ext.keys()).union(json_ext_minus_keys.keys())
#             for key in all_keys:
#                 value = head_json_ext.get(key)
#                 if value is None and key in group.json_ext:
#                     del group.json_ext[key]
#                 else:
#                     group.json_ext[key] = value

#         current_members = group.json_ext.get("members", {})
#         additional_members = {k: v for k, v in group_members.items() if k not in current_members}
#         remove_members = {k: v for k, v in current_members.items() if k not in group_members}
#         updated_members = {**current_members, **additional_members}
#         for member_id in remove_members:
#             updated_members.pop(member_id, None)

#         if current_members != updated_members:
#             changes_to_save["members"] = updated_members

#         if group.json_ext.get("head") != head_str:
#             changes_to_save["head"] = head_str

#         if group.json_ext.get("head_id") != head_id:
#             changes_to_save["head_id"] = head_id

#         if group.json_ext.get("primary_recipient") != primary_str:
#             changes_to_save["primary_recipient"] = primary_str

#         if group.json_ext.get("primary_recipient_id") != primary_id:
#             changes_to_save["primary_recipient_id"] = primary_id

#         if group.json_ext.get("secondary_recipient") != secondary_str:
#             changes_to_save["secondary_recipient"] = secondary_str

#         if group.json_ext.get("secondary_recipient_id") != secondary_id:
#             changes_to_save["secondary_recipient_id"] = secondary_id

#         if changes_to_save:
#             group.json_ext.update(changes_to_save)
#             group.save(update_fields=['json_ext'], user=self.user)

#     def handle_assure_primary_recipient_in_group(self, group, recipient_type):
#         """
#             Making sure that group has a head.
#         """
#         if recipient_type == GroupIndividual.RecipientType.PRIMARY:
#             return
#         self._assure_primary_recipient_in_group(group)

#     def ensure_location_consistent(self, group, individual, role):
#         if group.location_id == individual.location_id:
#             return

#         if role == GroupIndividual.Role.HEAD and group.location_id is None:
#             group.location_id = individual.location_id
#             group.save(user=self.user)
#         else:
#             individual.location_id = group.location_id
#             individual.save(user=self.user)

#     def _assure_primary_recipient_in_group(self, group):
#         group_individuals = GroupIndividual.objects.filter(group=group, is_deleted=False)
#         primary_exists = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.PRIMARY).exists()
#         head_exists = group_individuals.filter(role=GroupIndividual.Role.HEAD).exists()

#         if primary_exists:
#             return

#         new_primary = group_individuals.first()

#         if not new_primary:
#             return

#         new_primary.recipient_type = GroupIndividual.RecipientType.PRIMARY
#         if not head_exists:
#             new_primary.role = GroupIndividual.Role.HEAD
#         new_primary.save(user=self.user)

#     def _change_head(self, group_individual_id, group_id):
#         heads_queryset = GroupIndividual.objects.filter(group_id=group_id, role=GroupIndividual.Role.HEAD)
#         old_head = heads_queryset.exclude(id=group_individual_id).first()

#         if not old_head:
#             return

#         old_head.role = None
#         old_head.save(user=self.user)

#     def _change_primary(self, group_individual_id, group_id):
#         primaries_queryset = GroupIndividual.objects.filter(
#             group_id=group_id, recipient_type=GroupIndividual.RecipientType.PRIMARY
#         )
#         old_primary = primaries_queryset.exclude(id=group_individual_id).first()

#         if not old_primary:
#             return

#         old_primary.recipient_type = None
#         old_primary.save(user=self.user)


# class IndividualImportService:
#     import_loaders = {
#         # .csv
#         'text/csv': lambda f: pd.read_csv(f),
#         # .xlsx
#         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': lambda f: pd.read_excel(f),
#         # .xls
#         'application/vnd.ms-excel': lambda f: pd.read_excel(f),
#         # .ods
#         'application/vnd.oasis.opendocument.spreadsheet': lambda f: pd.read_excel(f),
#     }

#     def __init__(self, user):
#         super().__init__()
#         self.user = user

#     @register_service_signal('individual.import_individuals')
#     def import_individuals(self,
#                            import_file: InMemoryUploadedFile,
#                            workflow: WorkflowHandler,
#                            group_aggregation_column: str):
#         upload = self._save_sources(import_file)
#         self._create_individual_data_upload_records(workflow, upload, group_aggregation_column)
#         self._trigger_workflow(workflow, upload)
#         return {'success': True, 'data': {'upload_uuid': upload.uuid}}

#     @transaction.atomic
#     def _save_sources(self, import_file):
#         # Method separated as workflow execution must be independent of the atomic transaction.
#         upload = self._create_upload_entry(import_file.name)
#         dataframe = self._load_import_file(import_file)
#         self._validate_dataframe(dataframe)
#         self._save_data_source(dataframe, upload)
#         return upload

#     @transaction.atomic
#     def _create_individual_data_upload_records(self, workflow, upload, group_aggregation_column):
#         record = IndividualDataUploadRecords(
#             data_upload=upload,
#             workflow=workflow.name,
#             json_ext={"group_aggregation_column": group_aggregation_column}
#         )
#         record.save(user=self.user)

#     def validate_import_individuals(self, upload_id: uuid, individual_sources):
#         dataframe = load_dataframe(individual_sources)
#         validated_dataframe, invalid_items = self._validate_possible_individuals(
#             dataframe,
#             upload_id
#         )
#         return {'success': True, 'data': validated_dataframe, 'summary_invalid_items': invalid_items}

#     def synchronize_data_for_reporting(self, upload_id: uuid):
#         if 'opensearch_reports' in apps.app_configs:
#             from individual.documents import IndividualDocument

#             individuals = Individual.objects.filter(individualdatasource__upload=upload_id)
#             if not individuals:
#                 return

#             IndividualDocument().update(individuals, 'index')

#     @staticmethod
#     def process_chunk(
#         chunk,
#         properties,
#         unique_validations,
#         loc_name_code_district_ids_from_db,
#         user_allowed_loc_ids,
#         duplicate_village_name_code_tuples,
#     ):
#         validated_dataframe = []
#         check_location = 'location_name' in chunk.columns

#         for _, row in chunk.iterrows():
#             field_validation = {'row': row.to_dict(), 'validations': {}}
#             for field, field_properties in properties.items():

#                 # Validation Calculation
#                 if "validationCalculation" in field_properties and field in row:
#                     field_validation['validations'][field] = IndividualImportService._handle_validation_calculation(row, field, field_properties)

#                 # Uniqueness Check
#                 if "uniqueness" in field_properties and field in row:
#                     field_validation['validations'][f'{field}_uniqueness'] = IndividualImportService._handle_uniqueness(row, field, unique_validations)

#             if 'location_name' in chunk.columns:
#                 field_validation['validations']['location_name'] = (
#                     IndividualImportService._validate_location(
#                         row.location_name,
#                         row.location_code,
#                         loc_name_code_district_ids_from_db,
#                         user_allowed_loc_ids,
#                         duplicate_village_name_code_tuples,
#                     )
#                 )

#             validated_dataframe.append(field_validation)

#         return validated_dataframe

#     def _validate_possible_individuals(self, dataframe: DataFrame, upload_id: uuid):
#         schema_dict = json.loads(IndividualConfig.individual_schema)
#         properties = schema_dict.get("properties", {})

#         unique_fields = [field for field, props in properties.items() if "uniqueness" in props]
#         unique_validations = {}
#         if unique_fields:
#             unique_validations = {
#                 field: dataframe[field].duplicated(keep=False) 
#                 for field in unique_fields
#             }

#         check_location = 'location_name' in dataframe.columns
#         if check_location:
#             # Issue a single DB query instead of per row for efficiency
#             loc_name_code_district_ids_from_db = self._query_location_district_ids(dataframe)
#             user_allowed_loc_ids = LocationManager().get_allowed_ids(self.user)
#             duplicate_village_name_code_tuples = self._query_duplicate_village_name_code()
#         else:
#             loc_name_code_district_ids_from_db = None
#             user_allowed_loc_ids = None
#             duplicate_village_name_code_tuples = None

#         # TODO: Use ProcessPoolExecutor after resolving django dependency loading issue
#         validated_dataframe = IndividualImportService.process_chunk(
#             dataframe,
#             properties,
#             unique_validations,
#             loc_name_code_district_ids_from_db,
#             user_allowed_loc_ids,
#             duplicate_village_name_code_tuples,
#         )

#         self.save_validation_error_in_data_source_bulk(validated_dataframe)
#         invalid_items = fetch_summary_of_broken_items(upload_id)
#         return validated_dataframe, invalid_items

#     @staticmethod
#     def _query_location_district_ids(df):
#         unique_tuples = df[['location_name', 'location_code']].drop_duplicates()
#         query = Q()
#         for _, row in unique_tuples.iterrows():
#             query |= Q(name=row['location_name'], code=row['location_code'])
#         locations = Location.objects.filter(type="V", *filter_validity()).filter(query)
#         return {(loc.name, loc.code): loc.parent.parent.id for loc in locations}

#     @staticmethod
#     def _query_duplicate_village_name_code():
#         return (
#             Location.objects
#             .filter(type="V", *filter_validity())
#             .values('name', 'code')
#             .annotate(name_count=Count('id'))
#             .filter(name_count__gt=1)
#             .values_list('name', 'code')
#         )

#     @staticmethod
#     def _validate_location(
#         location_name,
#         location_code,
#         loc_name_code_district_ids_from_db,
#         user_allowed_loc_ids,
#         duplicate_village_name_code_tuples
#     ):
#         result = {
#             'field_name': 'location_name',
#         }
#         if (pd.isna(location_name) or location_name == "") and (pd.isna(location_code) or location_code == ""):
#             result['success'] = True
#         elif loc_name_code_district_ids_from_db is None and user_allowed_loc_ids is None:
#             result['success'] = True
#         elif (location_name, location_code) not in loc_name_code_district_ids_from_db:
#             result['success'] = False
#             result['note'] = f"Location with name '{location_name}' and code '{location_code}' is not valid. Please check the spelling against the list of locations in the system."
#         elif (location_name, location_code) in duplicate_village_name_code_tuples:
#             result['success'] = False
#             result['note'] = f"Location with name '{location_name}' and code '{location_code}' is ambiguous, because there are more than one location with this name and code found in the system."
#         elif loc_name_code_district_ids_from_db[(location_name, location_code)] not in user_allowed_loc_ids:
#             result['success'] = False
#             result['note'] = f"Location with name '{location_name}' and code '{location_code}' is outside the current user's location permissions."
#         else:
#             result['success'] = True
#         return result

#     @staticmethod
#     def _handle_uniqueness(row, field, unique_validations):
#         success = not unique_validations[field].loc[row.name]
#         result = {
#             "success": success,
#             "field_name": field,
#         }
#         if not success:
#             result["note"] = f"'{field}' Field value '{row[field]}' is duplicated"
#         return result

#     @staticmethod
#     def _handle_validation_calculation(row, field, field_properties):
#         validation_calculation = field_properties.get("validationCalculation", {}).get("name")
#         if not validation_calculation:
#             raise ValueError("Missing validation name")
#         calculation_uuid = IndividualConfig.validation_calculation_uuid
#         calculation = get_calculation_object(calculation_uuid)
#         result_row = calculation.calculate_if_active_for_object(
#             validation_calculation,
#             calculation_uuid,
#             field_name=field,
#             field_value=row[field],
#         )
#         return result_row

#     def _create_upload_entry(self, filename):
#         upload = IndividualDataSourceUpload(source_name=filename, source_type='individual import')
#         upload.save(username=self.user.login_name)
#         return upload

#     def _validate_dataframe(self, dataframe: pd.DataFrame):
#         if dataframe is None:
#             raise ValueError("Unknown error while loading import file")
#         if dataframe.empty:
#             raise ValueError("Import file is empty")

#     def _load_import_file(self, import_file) -> pd.DataFrame:
#         if import_file.content_type not in self.import_loaders:
#             raise ValueError("Unsupported content type: {}".format(import_file.content_type))

#         return self.import_loaders[import_file.content_type](import_file)

#     def _save_data_source(self, dataframe: pd.DataFrame, upload: IndividualDataSourceUpload):
#         data_source_objects = []

#         for _, row in dataframe.iterrows():
#             # row.to_json() gives a JSON string of the CSV columns
#             base = json.loads(row.to_json())

#             # If the CSV contains a 'json_ext' column, merge it (string or dict)
#             merged = dict(base)
#             raw_jx = base.get("json_ext")
#             if isinstance(raw_jx, str) and raw_jx.strip():
#                 try:
#                     jx = json.loads(raw_jx)
#                 except Exception:
#                     jx = {}
#             elif isinstance(raw_jx, dict):
#                 jx = raw_jx
#             else:
#                 jx = {}

#             # merge: explicit CSV columns win; json_ext adds additional keys
#             merged.update(jx)
#             merged.pop("json_ext", None)  # avoid nesting json_ext inside itself

#             ds = IndividualDataSource(
#                 upload=upload,
#                 json_ext=merged,
#                 validations={},
#                 user_created=self.user,
#                 user_updated=self.user,
#                 uuid=uuid.uuid4()
#             )
#             data_source_objects.append(ds)

#         IndividualDataSource.objects.bulk_create(data_source_objects)

#     # def _save_data_source(self, dataframe: pd.DataFrame, upload: IndividualDataSourceUpload):
#     #     data_source_objects = []
        
#     #     for _, row in dataframe.iterrows():
#     #         ds = IndividualDataSource(
#     #             upload=upload,
#     #             json_ext=json.loads(row.to_json()),
#     #             validations={},
#     #             user_created=self.user,
#     #             user_updated=self.user,
#     #             uuid=uuid.uuid4()
#     #         )
#     #         data_source_objects.append(ds)

#     #     IndividualDataSource.objects.bulk_create(data_source_objects)

# # individual/service.py (drop-in for _trigger_workflow)

# def _trigger_workflow(self,
#                       workflow: WorkflowHandler,
#                       upload: IndividualDataSourceUpload):
#     """
#     Run the given workflow and, if maker–checker is enabled, create the verification task
#     for this upload (import vs update decided by workflow name).

#     Adds a safe-preview contract:
#       - Only fields listed in IndividualConfig.workflow_preview_fields
#       - Drop non-scalar (dict/list) values to avoid React "object as child" errors
#     """
#     # Small helpers stay local to avoid touching public API surface
#     def _preview_fields():
#         pf = getattr(IndividualConfig, "workflow_preview_fields", None)
#         # sane defaults if config missing
#         return list(pf) if pf else ["first_name", "last_name", "dob",
#                                     "location_name", "location_code",
#                                     "group_code", "individual_role", "pmt_score"]

#     def _preview_opts():
#         # Anything not a plain scalar should be excluded from preview rows
#         return {
#             "preview_fields": _preview_fields(),
#             "preview_drop_non_scalars": True,   # dict/list -> skip
#             "preview_max_rows": 200,            # optional: keep table light
#         }

#     try:
#         # mark as triggered early to avoid races
#         upload.status = IndividualDataSourceUpload.Status.TRIGGERED
#         upload.save(username=self.user.login_name)

#         result = workflow.run({
#             'user_uuid': str(User.objects.get(username=self.user.login_name).id),
#             'upload_uuid': str(upload.uuid),
#         })

#         # explicit failure payload from workflow
#         if result and isinstance(result, dict) and result.get('success') is False:
#             raise ValueError(result.get('message', 'Unexpected error during the workflow execution'))

#         # ---- Maker–checker: create verification task (import/update) with safe preview ----
#         try:
#             wf_name = (getattr(workflow, "name", "") or "").lower()
#             is_update_flow = "update" in wf_name
#             opts = _preview_opts()

#             if is_update_flow and IndividualConfig.enable_maker_checker_for_individual_update:
#                 # Preferred: methods that accept preview_* kwargs
#                 try:
#                     self.create_task_with_update_valid_items(upload.uuid, **opts)
#                 except TypeError:
#                     # Backward-compatible fallback if kwargs not supported
#                     self.create_task_with_update_valid_items(upload.uuid)
#                 logger.info("Created UPDATE verification task for upload %s", upload.uuid)

#             elif (not is_update_flow) and IndividualConfig.enable_maker_checker_for_individual_upload:
#                 try:
#                     self.create_task_with_importing_valid_items(upload.uuid, **opts)
#                 except TypeError:
#                     self.create_task_with_importing_valid_items(upload.uuid)
#                 logger.info("Created IMPORT verification task for upload %s", upload.uuid)

#             else:
#                 logger.debug("Maker–checker disabled for this flow (%s); no verification task created.", wf_name)

#         except Exception:
#             logger.exception("Creating verification task failed for upload %s", upload.uuid)

#     except ValueError as e:
#         upload.status = IndividualDataSourceUpload.Status.FAIL
#         upload.error = {'workflow': str(e)}
#         upload.save(username=self.user.login_name)
#         return upload


#     # def _trigger_workflow(self,
#     #                       workflow: WorkflowHandler,
#     #                       upload: IndividualDataSourceUpload):
#     #     """
#     #     Run the given workflow and, if maker–checker is enabled, create the verification task
#     #     for this upload (import vs update decided by workflow name).
#     #     """
#     #     try:
#     #         # mark as triggered to avoid races
#     #         upload.status = IndividualDataSourceUpload.Status.TRIGGERED
#     #         upload.save(username=self.user.login_name)

#     #         result = workflow.run({
#     #             'user_uuid': str(User.objects.get(username=self.user.login_name).id),
#     #             'upload_uuid': str(upload.uuid),
#     #         })

#     #         # If the workflow returns an explicit failure dict, fail fast
#     #         if result and isinstance(result, dict) and result.get('success') is False:
#     #             raise ValueError(result.get('message', 'Unexpected error during the workflow execution'))

#     #         # ---- NEW: auto-create task for verification (maker–checker) ----
#     #         try:
#     #             wf_name = (getattr(workflow, "name", "") or "").lower()
#     #             is_update_flow = "update" in wf_name

#     #             if is_update_flow and IndividualConfig.enable_maker_checker_for_individual_update:
#     #                 # Update path
#     #                 self.create_task_with_update_valid_items(upload.uuid)
#     #                 logger.info("Created UPDATE verification task for upload %s", upload.uuid)
#     #             elif (not is_update_flow) and IndividualConfig.enable_maker_checker_for_individual_upload:
#     #                 # Import path
#     #                 self.create_task_with_importing_valid_items(upload.uuid)
#     #                 logger.info("Created IMPORT verification task for upload %s", upload.uuid)
#     #             else:
#     #                 logger.debug("Maker–checker disabled for this flow (%s); no verification task created.", wf_name)
#     #         except Exception:
#     #             logger.exception("Creating verification task failed for upload %s", upload.uuid)

#     #     except ValueError as e:
#     #         upload.status = IndividualDataSourceUpload.Status.FAIL
#     #         upload.error = {'workflow': str(e)}
#     #         upload.save(username=self.user.login_name)
#     #         return upload

#     def save_validation_error_in_data_source_bulk(self, validated_dataframe):
#         data_sources_to_update = []

#         for field_validation in validated_dataframe:
#             row = field_validation['row']
#             error_fields = []

#             for key, value in field_validation['validations'].items():
#                 if not value.get('success', False):
#                     error_fields.append({
#                         "field_name": value.get('field_name'),
#                         "note": value.get('note')
#                     })

#             data_sources_to_update.append(
#                 IndividualDataSource(
#                     id=row['id'],
#                     validations={'validation_errors': error_fields}
#                 )
#             )

#         if data_sources_to_update:
#             IndividualDataSource.objects.bulk_update(data_sources_to_update, ['validations'])

#     def create_task_with_importing_valid_items(self, upload_id: uuid):
#         if IndividualConfig.enable_maker_checker_for_individual_upload:
#             IndividualTaskCreatorService(self.user) \
#                 .create_task_with_importing_valid_items(upload_id)
#         else:
#             record = IndividualDataUploadRecords.objects.get(
#                 data_upload_id=upload_id,
#                 is_deleted=False
#             )
#             from individual.signals.on_validation_import_valid_items import IndividualItemsImportTaskCompletionEvent
#             IndividualItemsImportTaskCompletionEvent(
#                 IndividualConfig.validation_import_valid_items_workflow,
#                 record,
#                 record.data_upload.id,
#                 self.user
#             ).run_workflow()

#     def create_task_with_update_valid_items(self, upload_id: uuid):
#         # Resolve automatically if maker-checker not enabled
#         if IndividualConfig.enable_maker_checker_for_individual_update:
#             IndividualTaskCreatorService(self.user) \
#                 .create_task_with_update_valid_items(upload_id)
#         else:
#             record = IndividualDataUploadRecords.objects.get(
#                 data_upload_id=upload_id,
#                 is_deleted=False
#             )
#             from individual.signals.on_validation_import_valid_items import IndividualItemsUploadTaskCompletionEvent
#             IndividualItemsUploadTaskCompletionEvent(
#                 IndividualConfig.validation_upload_valid_items_workflow,
#                 record,
#                 record.data_upload.id,
#                 self.user
#             ).run_workflow()

#     # ----------------------------------------------------------------------
#     # UPDATED: link Individuals to Groups for a given upload with location alignment
#     # ----------------------------------------------------------------------
#     # @transaction.atomic
#     # def link_groups_for_upload_uuid(self, upload_id: uuid.UUID) -> dict:
#     #     """
#     #     For each IndividualDataSource in this upload that has an Individual and a group_code:
#     #       - create/find Group(code=group_code)
#     #       - align locations (group vs individual) before creating link
#     #       - ensure GroupIndividual exists
#     #       - role HEAD if json_ext.individual_role_code == '1'
#     #       - recipient_type PRIMARY if json_ext.hhrep == json_ext.individual_role_code
#     #       - refresh Group.json_ext
#     #     """
#     #     alignment = GroupAndGroupIndividualAlignmentService(self.user)
#     #     sources = (
#     #         IndividualDataSource.objects
#     #         .filter(upload_id=upload_id, is_deleted=False)
#     #         .exclude(individual_id__isnull=True)
#     #         .values('id', 'individual_id', 'json_ext')
#     #     )

#     #     created_groups = 0
#     #     touched_groups = set()
#     #     created_links = 0
#     #     updated_links = 0

#     #     for src in sources:
#     #         je = src.get('json_ext') or {}
#     #         group_code = (je.get('group_code') or "").strip()
#     #         if not group_code:
#     #             continue

#     #         role_code = str(je.get('individual_role_code') or je.get('relationship_to_head') or "").strip()
#     #         hhrep_code = str(je.get('hhrep') or "").strip()

#     #         # Ensure group exists
#     #         group = Group.objects.filter(code=group_code, is_deleted=False).first()
#     #         if not group:
#     #             group = Group(code=group_code, json_ext={})
#     #             group.save(user=self.user)
#     #             created_groups += 1
#     #         touched_groups.add(group.id)

#     #         individual_id = src['individual_id']
#     #         # Fetch individual (needed for location alignment)
#     #         individual = Individual.objects.filter(id=individual_id, is_deleted=False).first()
#     #         if not individual:
#     #             logger.warning("Skipping group link; missing Individual id=%s for upload=%s", individual_id, upload_id)
#     #             continue

#     #         # Decide desired role & whether group already has a head
#     #         desired_role = GroupIndividual.Role.HEAD if role_code == "1" else None
#     #         has_head = GroupIndividual.objects.filter(group=group, role=GroupIndividual.Role.HEAD, is_deleted=False).exists()

#     #         # Align locations BEFORE creating/updating the GroupIndividual to avoid validation errors
#     #         # If no head exists yet, we can treat this member as head for alignment if they are HEAD, or
#     #         # still allow alignment using their role intent; otherwise the helper preserves current group location.
#     #         role_for_alignment = desired_role if (desired_role == GroupIndividual.Role.HEAD or not has_head) else None
#     #         try:
#     #             alignment.ensure_location_consistent(group, individual, role_for_alignment)
#     #         except Exception:
#     #             logger.exception("Location alignment failed for group %s individual %s", group.code, individual_id)

#     #         gi = GroupIndividual.objects.filter(group=group, individual_id=individual_id, is_deleted=False).first()
#     #         desired_recipient = GroupIndividual.RecipientType.PRIMARY if (hhrep_code and hhrep_code == role_code) else None

#     #         if not gi:
#     #             gi = GroupIndividual(group=group, individual_id=individual_id)
#     #             gi.role = desired_role
#     #             gi.recipient_type = desired_recipient
#     #             gi.save(user=self.user)
#     #             created_links += 1
#     #         else:
#     #             changed = False
#     #             if gi.role != desired_role:
#     #                 gi.role = desired_role
#     #                 changed = True
#     #             if gi.recipient_type != desired_recipient:
#     #                 gi.recipient_type = desired_recipient
#     #                 changed = True
#     #             if changed:
#     #                 gi.save(user=self.user)
#     #                 updated_links += 1

#     #     for gid in touched_groups:
#     #         g = Group.objects.get(id=gid)
#     #         alignment.update_json_ext_for_group(g)

#     #     return {
#     #         "created_groups": created_groups,
#     #         "groups_touched": len(touched_groups),
#     #         "created_links": created_links,
#     #         "updated_links": updated_links,
#     #     }
# # individual/services.py  (inside class IndividualImportService)

# # @transaction.atomic
# # def link_groups_for_upload_uuid(self, upload_id: uuid.UUID) -> dict:
# #     """
# #     For each IndividualDataSource in this upload that has an Individual and a group_code:
# #       - normalize group_code if it's a short TF4 (use location_code + prefix/midfix)
# #       - create/find Group(code=group_code)
# #       - ensure GroupIndividual exists
# #       - role HEAD if role_code == '1' OR individual_role == 'HEAD'
# #       - recipient_type PRIMARY if hhrep == role_code OR (no hhrep but role == HEAD)
# #       - set Group.location_id from the head's Individual if group has no location yet
# #       - refresh Group.json_ext
# #     """
# #     from individual.apps import IndividualConfig  # local import to avoid circulars
# #     alignment = GroupAndGroupIndividualAlignmentService(self.user)

# #     def _digits(s: Any) -> str:
# #         return "".join(ch for ch in str(s or "") if ch.isdigit())

# #     def _normalize_group_code(je: dict) -> str | None:
# #         raw = str((je.get('group_code') or '')).strip()
# #         loc = _digits(je.get('location_code'))
# #         prefix = getattr(IndividualConfig, "group_code_prefix", "P3") or "P3"
# #         midfix = getattr(IndividualConfig, "group_code_midfix", "000") or "000"

# #         # Already a long P3... code -> keep as-is
# #         if raw.upper().startswith("P") and len(raw) >= 10:
# #             return raw

# #         # If it's a numeric TF4 and we have a location code -> compose the full code
# #         tf4 = _digits(raw)
# #         if tf4 and len(tf4) <= 4 and loc:
# #             loc9 = loc.zfill(9)  # RRDDWWVVV
# #             return f"{prefix}{loc9}{midfix}{tf4.zfill(4)}"

# #         # Otherwise return whatever is provided (may be short)
# #         return raw or None

# #     sources = (
# #         IndividualDataSource.objects
# #         .filter(upload_id=upload_id, is_deleted=False)
# #         .exclude(individual_id__isnull=True)
# #         .values('id', 'individual_id', 'json_ext')
# #     )

# #     created_groups = 0
# #     touched_groups = set()
# #     created_links = 0
# #     updated_links = 0
# #     location_updates = 0

# #     for src in sources:
# #         je = src.get('json_ext') or {}
# #         group_code = _normalize_group_code(je)
# #         if not group_code:
# #             continue

# #         # Role: prefer numeric code, fallback to label
# #         role_code = str(je.get('individual_role_code') or je.get('relationship_to_head') or "").strip()
# #         role_label = str(je.get('individual_role') or "").strip().upper()
# #         hhrep_code = str(je.get('hhrep') or "").strip()

# #         desired_role = GroupIndividual.Role.HEAD if (role_code == "1" or role_label == "HEAD") else None
# #         desired_recipient = None
# #         if hhrep_code and role_code and hhrep_code == role_code:
# #             desired_recipient = GroupIndividual.RecipientType.PRIMARY
# #         elif desired_role == GroupIndividual.Role.HEAD and not hhrep_code:
# #             # sensible default: head is primary if hhrep is missing
# #             desired_recipient = GroupIndividual.RecipientType.PRIMARY

# #         group = Group.objects.filter(code=group_code, is_deleted=False).first()
# #         if not group:
# #             group = Group(code=group_code, json_ext={})
# #             group.save(user=self.user)
# #             created_groups += 1
# #         touched_groups.add(group.id)

# #         individual_id = src['individual_id']
# #         gi = GroupIndividual.objects.filter(group=group, individual_id=individual_id, is_deleted=False).first()

# #         if not gi:
# #             gi = GroupIndividual(group=group, individual_id=individual_id)
# #             gi.role = desired_role
# #             gi.recipient_type = desired_recipient
# #             gi.save(user=self.user)
# #             created_links += 1
# #         else:
# #             changed = False
# #             if gi.role != desired_role:
# #                 gi.role = desired_role
# #                 changed = True
# #             if gi.recipient_type != desired_recipient:
# #                 gi.recipient_type = desired_recipient
# #                 changed = True
# #             if changed:
# #                 gi.save(user=self.user)
# #                 updated_links += 1

# #         # If group has no location, adopt the head/member's location
# #         if not group.location_id:
# #             ind = Individual.objects.filter(id=individual_id).first()
# #             if ind and ind.location_id:
# #                 group.location_id = ind.location_id
# #                 group.save(user=self.user)
# #                 location_updates += 1

# #     for gid in touched_groups:
# #         g = Group.objects.get(id=gid)
# #         alignment.update_json_ext_for_group(g)

# #     return {
# #         "created_groups": created_groups,
# #         "groups_touched": len(touched_groups),
# #         "created_links": created_links,
# #         "updated_links": updated_links,
# #         "groups_location_set": location_updates,
# #     }


#     @transaction.atomic
#     def link_groups_for_upload_uuid(self, upload_id: uuid.UUID) -> dict:
#         """
#         For each IndividualDataSource in this upload that has an Individual and a group_code:
#           - If group_code already matches the canonical P3 format, use it as-is.
#           - Else, try to normalize short/legacy codes using location_code (+ TF4 if available).
#           - Create/find Group(code=group_code)
#           - Ensure GroupIndividual exists
#           - role HEAD if json_ext.individual_role_code == '1' OR json_ext.individual_role == 'HEAD'
#           - recipient_type PRIMARY if json_ext.hhrep == json_ext.individual_role_code, or if hhrep missing and member is HEAD
#           - If Group.location_id is missing, set it from the member's location
#           - Refresh Group.json_ext (members, head/primary/secondary, carry head's json_ext)
#         """
#         alignment = GroupAndGroupIndividualAlignmentService(self.user)

#         sources = (
#             IndividualDataSource.objects
#             .filter(upload_id=upload_id, is_deleted=False)
#             .exclude(individual_id__isnull=True)
#             .values('id', 'individual_id', 'json_ext')
#         )

#         def _digits(val: str | None) -> str:
#             return "".join(ch for ch in str(val or "") if ch.isdigit())

#         def _is_valid_p3_code(code: str | None) -> bool:
#             """Expect 'P3' + 9-digit location + '000' + 4-digit TF4 → total length 18."""
#             s = (code or "").strip()
#             if not s.startswith("P3"):
#                 return False
#             # P3 + RRDDWWVVV (9) + 000 (3) + TTTT (4) = 18
#             return len(s) == 18 and s[2:11].isdigit() and s[11:14] == "000" and s[14:18].isdigit()

#         def _compose_p3_code(jx: dict) -> str | None:
#             """
#             Try building a canonical code from json_ext when a legacy/short code was provided.
#             Needs a 9-digit location_code; TF4 (tf4_no) is optional but preferred.
#             """
#             loc_code = str(jx.get("location_code") or "").strip()
#             tf4_raw = jx.get("tf4_no") or jx.get("TF4_NO") or ""
#             tf4 = _digits(str(tf4_raw)).zfill(4) if str(tf4_raw).strip() else None

#             if not loc_code:
#                 return None

#             loc_digits = _digits(loc_code)
#             if not loc_digits:
#                 return None

#             # zero-pad to 9 digits (RRDDWWVVV)
#             if not loc_digits.isdigit():
#                 return None
#             loc9 = loc_digits.zfill(9)

#             # If we don't have TF4, we can still construct "P3 + loc9" (non-canonical but stable)
#             # Prefer canonical with midfix '000' and a 4-digit TF4 when available.
#             if tf4:
#                 return f"P3{loc9}000{tf4}"
#             return f"P3{loc9}"

#         created_groups = 0
#         touched_groups: set[str] = set()
#         created_links = 0
#         updated_links = 0
#         set_locations = 0

#         for src in sources:
#             je = src.get("json_ext") or {}
#             if not isinstance(je, dict):
#                 continue

#             raw_gc = (je.get("group_code") or "").strip()
#             if not raw_gc:
#                 # no group code → skip linking
#                 continue

#             # 1) Resolve the effective group code
#             if _is_valid_p3_code(raw_gc):
#                 eff_code = raw_gc
#             else:
#                 # Try normalize legacy/short values using row context
#                 eff_code = _compose_p3_code(je) or raw_gc  # fall back to given value if we can't compose

#             # 2) Find or create the group
#             group = Group.objects.filter(code=eff_code, is_deleted=False).first()
#             if not group:
#                 group = Group(code=eff_code, json_ext={})
#                 group.save(user=self.user)
#                 created_groups += 1
#             touched_groups.add(str(group.id))

#             # 3) Ensure location on the group (if missing)
#             if group.location_id is None:
#                 try:
#                     ind = Individual.objects.get(id=src["individual_id"])
#                 except Individual.DoesNotExist:
#                     ind = None
#                 if ind and ind.location_id:
#                     group.location_id = ind.location_id
#                     group.save(user=self.user)
#                     set_locations += 1

#             # 4) Decide role/recipient
#             role_code = str(je.get("individual_role_code") or je.get("relationship_to_head") or "").strip()
#             role_label = str(je.get("individual_role") or "").strip().upper()
#             is_head = (role_code == "1") or (role_label == "HEAD")

#             hhrep_code = str(je.get("hhrep") or "").strip()

#             desired_role = GroupIndividual.Role.HEAD if is_head else None
#             desired_recipient = None
#             if hhrep_code and role_code and hhrep_code == role_code:
#                 desired_recipient = GroupIndividual.RecipientType.PRIMARY
#             elif is_head and not hhrep_code:
#                 # sensible default when hhrep missing: make head the primary recipient
#                 desired_recipient = GroupIndividual.RecipientType.PRIMARY

#             # 5) Ensure GroupIndividual exists + is up-to-date
#             individual_id = src["individual_id"]
#             gi = GroupIndividual.objects.filter(group=group, individual_id=individual_id, is_deleted=False).first()

#             if not gi:
#                 gi = GroupIndividual(group=group, individual_id=individual_id)
#                 gi.role = desired_role
#                 gi.recipient_type = desired_recipient
#                 gi.save(user=self.user)
#                 created_links += 1
#             else:
#                 changed = False
#                 if gi.role != desired_role:
#                     gi.role = desired_role
#                     changed = True
#                 if gi.recipient_type != desired_recipient:
#                     gi.recipient_type = desired_recipient
#                     changed = True
#                 if changed:
#                     gi.save(user=self.user)
#                     updated_links += 1

#         # 6) Refresh json_ext for all touched groups
#         for gid in touched_groups:
#             g = Group.objects.get(id=gid)
#             alignment.update_json_ext_for_group(g)

#         return {
#             "created_groups": created_groups,
#             "groups_touched": len(touched_groups),
#             "created_links": created_links,
#             "updated_links": updated_links,
#             "groups_location_set": set_locations,
#         }

# class IndividualTaskCreatorService:

#     def __init__(self, user):
#         self.user = user

#     def create_task_with_importing_valid_items(self, upload_id: uuid):
#         self._create_task(upload_id, IndividualConfig.validation_import_valid_items)

#     def create_task_with_update_valid_items(self, upload_id: uuid):
#         self._create_task(upload_id, IndividualConfig.validation_upload_valid_items)

#     @register_service_signal('individual.update_task')
#     @transaction.atomic()
#     def _create_task(self, upload_id, business_event):
#         from tasks_management.services import TaskService
#         from tasks_management.apps import TasksManagementConfig
#         from tasks_management.models import Task
#         upload_record = IndividualDataUploadRecords.objects.get(
#             data_upload_id=upload_id,
#             is_deleted=False
#         )
#         json_ext = {
#             'source_name': upload_record.data_upload.source_name,
#             'workflow': upload_record.workflow,
#             'percentage_of_invalid_items': self.__calculate_percentage_of_invalid_items(upload_id),
#             'data_upload_id': str(upload_id),
#             'group_aggregation_column':
#                 upload_record.json_ext.get('group_aggregation_column')
#                 if isinstance(upload_record.json_ext, dict)
#                 else None,
#         }
#         TaskService(self.user).create({
#             'source': 'import_valid_items',
#             'entity': upload_record,
#             'status': Task.Status.RECEIVED,
#             'executor_action_event': TasksManagementConfig.default_executor_event,
#             'business_event': business_event,
#             'json_ext': json_ext
#         })

#         data_upload = upload_record.data_upload
#         data_upload.status = IndividualDataSourceUpload.Status.WAITING_FOR_VERIFICATION
#         data_upload.save(user=self.user)

#     def __calculate_percentage_of_invalid_items(self, upload_id):
#         number_of_valid_items = len(fetch_summary_of_valid_items(upload_id))
#         number_of_invalid_items = len(fetch_summary_of_broken_items(upload_id))
#         total_items = number_of_invalid_items + number_of_valid_items

#         if total_items == 0:
#             percentage_of_invalid_items = 0
#         else:
#             percentage_of_invalid_items = (number_of_invalid_items / total_items) * 100

#         percentage_of_invalid_items = round(percentage_of_invalid_items, 2)
#         return percentage_of_invalid_items


# # import logging
# # import json
# # import uuid
# # import pandas as pd
# # import concurrent.futures
# # import math
# # from pandas import DataFrame
# # from django.core.files.uploadedfile import InMemoryUploadedFile
# # from django.db import transaction

# # from calculation.services import get_calculation_object
# # from core import filter_validity
# # from core.custom_filters import CustomFilterWizardStorage
# # from core.models import User
# # from core.services import BaseService
# # from core.signals import register_service_signal
# # from django.apps import apps
# # from django.utils.translation import gettext as _
# # from django.db.models import Q, OuterRef, Subquery, Count
# # from individual.apps import IndividualConfig
# # from individual.models import (
# #     Individual,
# #     IndividualDataSource,
# #     GroupIndividual,
# #     Group,
# #     IndividualDataUploadRecords,
# #     IndividualDataSourceUpload
# # )
# # from individual.utils import (
# #     load_dataframe,
# #     fetch_summary_of_valid_items,
# #     fetch_summary_of_broken_items
# # )
# # from individual.validation import (
# #     IndividualValidation,
# #     IndividualDataSourceValidation,
# #     GroupIndividualValidation,
# #     GroupValidation, CrateGroupAndMoveIndividualValidation
# # )
# # from core.services.utils import check_authentication as check_authentication, output_exception, output_result_success, \
# #     model_representation
# # from location.models import Location, LocationManager
# # from tasks_management.models import Task
# # from tasks_management.services import UpdateCheckerLogicServiceMixin, CreateCheckerLogicServiceMixin, \
# #     crud_business_data_builder, DeleteCheckerLogicServiceMixin
# # from workflow.systems.base import WorkflowHandler

# # # NEW: tolerant parsing for schema that may be JSON string OR (Ordered)dict
# # from collections import OrderedDict

# # logger = logging.getLogger(__name__)


# # def _safe_parse_schema(raw):
# #     """
# #     Accept ModuleConfiguration-backed schema in either string or mapping form.
# #     Returns a plain dict; {} on failure.
# #     """
# #     if isinstance(raw, (str, bytes, bytearray)):
# #         try:
# #             return json.loads(raw)
# #         except Exception:
# #             return {}
# #     if isinstance(raw, (dict, OrderedDict)):
# #         try:
# #             return dict(raw)
# #         except Exception:
# #             return {}
# #     return {}


# # class IndividualService(BaseService, UpdateCheckerLogicServiceMixin, DeleteCheckerLogicServiceMixin):
# #     @register_service_signal('individual_service.create')
# #     def create(self, obj_data):
# #         return super().create(obj_data)

# #     def create_update_task(self, obj_data):
# #         self._update_json_ext(obj_data)
# #         return super().create_update_task(obj_data)

# #     @register_service_signal('individual_service.update')
# #     def update(self, obj_data):
# #         self._update_json_ext(obj_data)
# #         return super().update(obj_data)

# #     @register_service_signal('individual_service.delete')
# #     def delete(self, obj_data):
# #         return super().delete(obj_data)

# #     @register_service_signal('individual_service.undo_delete')
# #     @check_authentication
# #     def undo_delete(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 self.validation_class.validate_undo_delete(obj_data)
# #                 obj_ = self.OBJECT_TYPE.objects.filter(id=obj_data['id']).first()
# #                 obj_.is_deleted = False
# #                 obj_.save(user=self.user)
# #                 return {
# #                     "success": True,
# #                     "message": "Ok",
# #                     "detail": "Undo Delete",
# #                 }
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="undo_delete", exception=exc)

# #     @register_service_signal('individual_service.select_individuals_to_benefit_plan')
# #     def select_individuals_to_benefit_plan(self, custom_filters, benefit_plan_id, status, user):
# #         individual_query = Individual.objects.filter(is_deleted=False)
# #         subquery = GroupIndividual.objects.filter(
# #             individual=OuterRef('pk')
# #         ).exclude(
# #             is_deleted=True
# #         ).values('individual')
# #         individual_query_with_filters = CustomFilterWizardStorage.build_custom_filters_queryset(
# #             "individual",
# #             "Individual",
# #             custom_filters,
# #             individual_query,
# #         )
# #         individual_query_with_filters = individual_query_with_filters.filter(~Q(pk__in=Subquery(subquery))).distinct()
# #         if benefit_plan_id:
# #             individuals_assigned_to_selected_programme = individual_query_with_filters. \
# #                 filter(is_deleted=False, beneficiary__benefit_plan_id=benefit_plan_id)
# #             individuals_not_assigned_to_selected_programme = individual_query_with_filters.exclude(
# #                 id__in=individuals_assigned_to_selected_programme.values_list('id', flat=True)
# #             )
# #             output = {
# #                 "individuals_assigned_to_selected_programme": individuals_assigned_to_selected_programme,
# #                 "individuals_not_assigned_to_selected_programme": individuals_not_assigned_to_selected_programme,
# #                 "individual_query_with_filters": individual_query_with_filters,
# #                 "benefit_plan_id": benefit_plan_id,
# #                 "status": status,
# #                 "user": user,
# #             }
# #             return output
# #         return None

# #     @register_service_signal('individual_service.create_accept_enrolment_task')
# #     def create_accept_enrolment_task(self, individual_queryset, benefit_plan_id):
# #         pass

# #     def _update_json_ext(self, obj_data):
# #         if not obj_data or 'json_ext' not in obj_data or 'location_id' not in obj_data:
# #             return

# #         json_ext = obj_data['json_ext']
# #         if not json_ext:
# #             return

# #         location_id = obj_data['location_id']
# #         if location_id:
# #             location = Location.objects.get(id=location_id)
# #             json_ext['location_str'] = str(location)
# #         else:
# #             json_ext['location_str'] = None

# #         obj_data['json_ext'] = json_ext

# #     OBJECT_TYPE = Individual

# #     def __init__(self, user, validation_class=IndividualValidation):
# #         super().__init__(user, validation_class)


# # class IndividualDataSourceService(BaseService):
# #     @register_service_signal('individual_data_source_service.create')
# #     def create(self, obj_data):
# #         return super().create(obj_data)

# #     @register_service_signal('individual_data_source_service.update')
# #     def update(self, obj_data):
# #         return super().update(obj_data)

# #     @register_service_signal('individual_data_source_service.delete')
# #     def delete(self, obj_data):
# #         return super().delete(obj_data)

# #     OBJECT_TYPE = IndividualDataSource

# #     def __init__(self, user, validation_class=IndividualDataSourceValidation):
# #         super().__init__(user, validation_class)


# # class GroupService(
# #     BaseService,
# #     CreateCheckerLogicServiceMixin,
# #     UpdateCheckerLogicServiceMixin,
# #     DeleteCheckerLogicServiceMixin
# # ):
# #     OBJECT_TYPE = Group

# #     def __init__(self, user, validation_class=GroupValidation):
# #         super().__init__(user, validation_class)

# #     @check_authentication
# #     @register_service_signal('group_service.create')
# #     def create(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 individuals_data = obj_data.pop('individuals_data', None)
# #                 result = super().create(obj_data)
# #                 group_id = result.get('data', {}).get('id')

# #                 if not group_id:
# #                     return result

# #                 if individuals_data:
# #                     individual_ids = [data["individual_id"] for data in individuals_data]
# #                     self._update_group_json_ext(group_id, individual_ids)
# #                     for data in individuals_data:
# #                         obj_data = {
# #                             'group_id': group_id,
# #                             'individual_id': data.get("individual_id"),
# #                             'role': data.get("role"),
# #                             'recipient_type': data.get("recipient_type")
# #                         }
# #                         service = GroupIndividualService(self.user)
# #                         service.create(obj_data)
# #                 return result
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

# #     @check_authentication
# #     @register_service_signal('group_service.update')
# #     def update(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 individuals_data = obj_data.pop('individuals_data', None)
# #                 result = super().update(obj_data)

# #                 if not individuals_data:
# #                     return result

# #                 group_id = obj_data['id']
# #                 assigned_individuals_ids = \
# #                     GroupIndividual.objects.filter(group_id=group_id).values_list('individual_id', flat=True)

# #                 service = GroupIndividualService(self.user)
# #                 individual_ids = [data['individual_id'] for data in individuals_data]
# #                 group = self._update_group_json_ext(group_id, individual_ids)

# #                 for individual_id in assigned_individuals_ids:
# #                     if str(individual_id) not in individual_ids:
# #                         group_individual = GroupIndividual.objects.get(group_id=group_id, individual_id=individual_id)
# #                         service.delete({'id': group_individual.id})

# #                 for data in individuals_data:
# #                     if uuid.UUID(data["individual_id"]) not in assigned_individuals_ids:
# #                         obj_data = {
# #                             'group_id': group_id,
# #                             'individual_id': data.get("individual_id"),
# #                             'role': data.get("role"),
# #                             'recipient_type': data.get("recipient_type")
# #                         }
# #                         service.create(obj_data)

# #                 dict_repr = model_representation(group)
# #                 return output_result_success(dict_representation=dict_repr)
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

# #     @register_service_signal('group_service.delete')
# #     def delete(self, obj_data):
# #         # if there ever was a requirement to undo group delete, remember to use members from json_ext, you will avoid
# #         # adding individuals that had been deleted from the group before group deletion
# #         with transaction.atomic():
# #             group_id = obj_data.get('id')
# #             group_individuals = GroupIndividual.objects.filter(group_id=group_id)
# #             for group_individual in group_individuals:
# #                 # cant use .delete() on query since it will completely remove instances from db instead of marking
# #                 # them as isDeleted
# #                 group_individual.delete(user=self.user)
# #             return super().delete(obj_data)

# #     @transaction.atomic
# #     def _update_group_json_ext(self, group_id, individual_ids):
# #         # it makes sure GroupIndividual .save() won't add each individual separately to group json_ext
# #         # because their ids will be already there
# #         group = Group.objects.get(id=group_id)
# #         group_members = {
# #             str(individual.id): f"{individual.first_name} {individual.last_name}"
# #             for individual in Individual.objects.filter(id__in=individual_ids)
# #         }
# #         group.json_ext["members"] = group_members
# #         group.save(user=self.user)
# #         return group

# #     @register_service_signal('group_service.select_groups_to_benefit_plan')
# #     def select_groups_to_benefit_plan(self, custom_filters, benefit_plan_id, status, user):
# #         group_query = Group.objects.filter(is_deleted=False)
# #         # criteria will be based on head of the group
# #         group_query_with_filters = CustomFilterWizardStorage.build_custom_filters_queryset(
# #             "individual",
# #             "Group",
# #             custom_filters,
# #             group_query,
# #         )
# #         if benefit_plan_id:
# #             groups_assigned_to_selected_programme = group_query_with_filters. \
# #                 filter(is_deleted=False, groupbeneficiary__benefit_plan_id=benefit_plan_id)
# #             groups_not_assigned_to_selected_programme = group_query_with_filters.exclude(
# #                 id__in=groups_assigned_to_selected_programme.values_list('id', flat=True)
# #             )
# #             output = {
# #                 "groups_assigned_to_selected_programme": groups_assigned_to_selected_programme,
# #                 "groups_not_assigned_to_selected_programme": groups_not_assigned_to_selected_programme,
# #                 "group_query_with_filters": group_query_with_filters,
# #                 "benefit_plan_id": benefit_plan_id,
# #                 "status": status,
# #                 "user": user,
# #             }
# #             return output
# #         return None


# # class CreateGroupAndMoveIndividualService(CreateCheckerLogicServiceMixin):
# #     OBJECT_TYPE = Group

# #     def __init__(self, user, validation_class=CrateGroupAndMoveIndividualValidation):
# #         self.user = user
# #         self.validation_class = validation_class

# #     @check_authentication
# #     @register_service_signal('create_group_and_move_individual.create')
# #     def create(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 self.validation_class.validate_create_group_and_move_individual(self.user, **obj_data)
# #                 group_individual_id = obj_data.pop('group_individual_id')
# #                 group = GroupService(self.user).create(obj_data)
# #                 # return group if it has errors
# #                 if not group['data']:
# #                     return group
# #                 group_individual = GroupIndividual.objects.filter(id=group_individual_id).first()
# #                 group_id = group['data']['id']
# #                 service = GroupIndividualService(self.user)
# #                 service.update({
# #                     'group_id': group_id, "id": group_individual_id, "role": group_individual.role
# #                 })
# #                 group_and_individuals_message = {**group, 'detail': group_individual_id}
# #                 return group_and_individuals_message
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

# #     def _business_data_serializer(self, data):
# #         def serialize(key, value):
# #             if key == 'group_individual_id':
# #                 group_individual = GroupIndividual.objects.get(id=value)
# #                 return f'{group_individual.individual.first_name} {group_individual.individual.last_name}'
# #             return value

# #         serialized_data = crud_business_data_builder(data, serialize)
# #         # TODO change to group code
# #         serialized_data['incoming_data']["id"] = 'NEW_GROUP'
# #         return serialized_data


# # class GroupIndividualService(BaseService, UpdateCheckerLogicServiceMixin):
# #     OBJECT_TYPE = GroupIndividual

# #     def __init__(self, user, validation_class=GroupIndividualValidation):
# #         super().__init__(user, validation_class)

# #     @register_service_signal('groupindividual_service.create')
# #     def create(self, obj_data):
# #         return super().create(obj_data)

# #     @check_authentication
# #     @register_service_signal('groupindividual_service.update')
# #     def update(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 group_individual_id = obj_data.get('id')
# #                 incoming_group_id = obj_data.get('group_id')
# #                 group_individual = GroupIndividual.objects.filter(id=group_individual_id, is_deleted=False).first()
# #                 if not group_individual:
# #                     raise ValueError(f"no GroupIndividual found with this id {group_individual_id}")

# #                 if str(group_individual.group.id) == str(incoming_group_id):
# #                     return super().update(obj_data)

# #                 obj_data.pop('id', None)
# #                 obj_data.pop('recipient_type', None)
# #                 obj_data.pop('role', None)
# #                 result = self.create(obj_data)
# #                 self.delete({'id': group_individual_id})
# #                 return result
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

# #     @register_service_signal('groupindividual_service.delete')
# #     def delete(self, obj_data):
# #         return super().delete(obj_data)

# #     def _business_data_serializer(self, data):
# #         def serialize(key, value):
# #             if key == 'id':
# #                 group_individual = GroupIndividual.objects.get(id=value)
# #                 return f'{group_individual.individual.first_name} {group_individual.individual.last_name}'
# #             if key == 'group_id':
# #                 group = Group.objects.get(id=value)
# #                 return group.code
# #             return value

# #         serialized_data = crud_business_data_builder(data, serialize)
# #         return serialized_data


# # class GroupAndGroupIndividualAlignmentService:
# #     """
# #         Service used in overridden .save() of GroupIndividual model.
# #     """

# #     def __init__(self, user):
# #         self.user = user

# #     def handle_head_change(self, group_individual_id, role, group_id):
# #         """
# #             Method used for making sure that during head change, the old one is set to default role.
# #         """
# #         if role == GroupIndividual.Role.HEAD:
# #             self._change_head(group_individual_id, group_id)

# #     def handle_primary_recipient_change(self, group_individual_id, recipient_type, group_id):
# #         """
# #             Method used for making sure that during primary recipient change, the old one is set to default role.
# #         """
# #         if recipient_type == GroupIndividual.RecipientType.PRIMARY:
# #             self._change_primary(group_individual_id, group_id)

# #     def update_json_ext_for_group(self, group):
# #         """
# #         This method ensures that json_ext of a group is up-to-date with its roles and members.
# #         """
# #         group_individuals = GroupIndividual.objects.filter(group_id=group.id, is_deleted=False)
# #         head = group_individuals.filter(role=GroupIndividual.Role.HEAD).first()
# #         primary = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.PRIMARY).first()
# #         secondary = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.SECONDARY).first()

# #         group_members = {
# #             str(individual.individual.id): f"{individual.individual.first_name} {individual.individual.last_name}"
# #             for individual in group_individuals
# #         }

# #         head_str = f'{head.individual.first_name} {head.individual.last_name}' if head else None
# #         head_id = str(head.individual.id) if head else None
# #         head_json_ext = head.individual.json_ext if head and head.individual.json_ext else {}

# #         primary_str = f'{primary.individual.first_name} {primary.individual.last_name}' if primary else None
# #         primary_id = str(primary.individual.id) if primary else None

# #         secondary_str = f'{secondary.individual.first_name} {secondary.individual.last_name}' if secondary else None
# #         secondary_id = str(secondary.individual.id) if secondary else None

# #         changes_to_save = {}
# #         json_ext_minus_keys = {k: v for k, v in group.json_ext.items() if k not in [
# #             "members", "head", "head_id", "primary_recipient",
# #             "primary_recipient_id", "secondary_recipient", "secondary_recipient_id"
# #         ]}

# #         if json_ext_minus_keys != head_json_ext:
# #             all_keys = set(head_json_ext.keys()).union(json_ext_minus_keys.keys())
# #             for key in all_keys:
# #                 value = head_json_ext.get(key)
# #                 if value is None and key in group.json_ext:
# #                     del group.json_ext[key]
# #                 else:
# #                     group.json_ext[key] = value

# #         current_members = group.json_ext.get("members", {})
# #         additional_members = {k: v for k, v in group_members.items() if k not in current_members}
# #         remove_members = {k: v for k, v in current_members.items() if k not in group_members}
# #         updated_members = {**current_members, **additional_members}
# #         for member_id in remove_members:
# #             updated_members.pop(member_id, None)

# #         if current_members != updated_members:
# #             changes_to_save["members"] = updated_members

# #         if group.json_ext.get("head") != head_str:
# #             changes_to_save["head"] = head_str

# #         if group.json_ext.get("head_id") != head_id:
# #             changes_to_save["head_id"] = head_id

# #         if group.json_ext.get("primary_recipient") != primary_str:
# #             changes_to_save["primary_recipient"] = primary_str

# #         if group.json_ext.get("primary_recipient_id") != primary_id:
# #             changes_to_save["primary_recipient_id"] = primary_id

# #         if group.json_ext.get("secondary_recipient") != secondary_str:
# #             changes_to_save["secondary_recipient"] = secondary_str

# #         if group.json_ext.get("secondary_recipient_id") != secondary_id:
# #             changes_to_save["secondary_recipient_id"] = secondary_id

# #         if changes_to_save:
# #             group.json_ext.update(changes_to_save)
# #             group.save(update_fields=['json_ext'], user=self.user)

# #     def handle_assure_primary_recipient_in_group(self, group, recipient_type):
# #         """
# #             Making sure that group has a head.
# #         """
# #         if recipient_type == GroupIndividual.RecipientType.PRIMARY:
# #             return
# #         self._assure_primary_recipient_in_group(group)

# #     def ensure_location_consistent(self, group, individual, role):
# #         if group.location_id == individual.location_id:
# #             return

# #         if role == GroupIndividual.Role.HEAD and group.location_id is None:
# #             group.location_id = individual.location_id
# #             group.save(user=self.user)
# #         else:
# #             individual.location_id = group.location_id
# #             individual.save(user=self.user)

# #     def _assure_primary_recipient_in_group(self, group):
# #         group_individuals = GroupIndividual.objects.filter(group=group, is_deleted=False)
# #         primary_exists = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.PRIMARY).exists()
# #         head_exists = group_individuals.filter(role=GroupIndividual.Role.HEAD).exists()

# #         if primary_exists:
# #             return

# #         new_primary = group_individuals.first()

# #         if not new_primary:
# #             return

# #         new_primary.recipient_type = GroupIndividual.RecipientType.PRIMARY
# #         if not head_exists:
# #             new_primary.role = GroupIndividual.Role.HEAD
# #         new_primary.save(user=self.user)

# #     def _change_head(self, group_individual_id, group_id):
# #         heads_queryset = GroupIndividual.objects.filter(group_id=group_id, role=GroupIndividual.Role.HEAD)
# #         old_head = heads_queryset.exclude(id=group_individual_id).first()

# #         if not old_head:
# #             return

# #         old_head.role = None
# #         old_head.save(user=self.user)

# #     def _change_primary(self, group_individual_id, group_id):
# #         primaries_queryset = GroupIndividual.objects.filter(
# #             group_id=group_id, recipient_type=GroupIndividual.RecipientType.PRIMARY
# #         )
# #         old_primary = primaries_queryset.exclude(id=group_individual_id).first()

# #         if not old_primary:
# #             return

# #         old_primary.recipient_type = None
# #         old_primary.save(user=self.user)


# # class IndividualImportService:
# #     import_loaders = {
# #         # .csv
# #         'text/csv': lambda f: pd.read_csv(f),
# #         # .xlsx
# #         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': lambda f: pd.read_excel(f),
# #         # .xls
# #         'application/vnd.ms-excel': lambda f: pd.read_excel(f),
# #         # .ods
# #         'application/vnd.oasis.opendocument.spreadsheet': lambda f: pd.read_excel(f),
# #     }

# #     def __init__(self, user):
# #         super().__init__()
# #         self.user = user

# #     @register_service_signal('individual.import_individuals')
# #     def import_individuals(self,
# #                            import_file: InMemoryUploadedFile,
# #                            workflow: WorkflowHandler,
# #                            group_aggregation_column: str):
# #         upload = self._save_sources(import_file)
# #         self._create_individual_data_upload_records(workflow, upload, group_aggregation_column)
# #         self._trigger_workflow(workflow, upload)
# #         return {'success': True, 'data': {'upload_uuid': upload.uuid}}

# #     @transaction.atomic
# #     def _save_sources(self, import_file):
# #         # Method separated as workflow execution must be independent of the atomic transaction.
# #         upload = self._create_upload_entry(import_file.name)
# #         dataframe = self._load_import_file(import_file)
# #         self._validate_dataframe(dataframe)
# #         self._save_data_source(dataframe, upload)
# #         return upload

# #     @transaction.atomic
# #     def _create_individual_data_upload_records(self, workflow, upload, group_aggregation_column):
# #         record = IndividualDataUploadRecords(
# #             data_upload=upload,
# #             workflow=workflow.name,
# #             json_ext={"group_aggregation_column": group_aggregation_column}
# #         )
# #         record.save(user=self.user)

# #     def validate_import_individuals(self, upload_id: uuid, individual_sources):
# #         dataframe = load_dataframe(individual_sources)
# #         validated_dataframe, invalid_items = self._validate_possible_individuals(
# #             dataframe,
# #             upload_id
# #         )
# #         return {'success': True, 'data': validated_dataframe, 'summary_invalid_items': invalid_items}

# #     def synchronize_data_for_reporting(self, upload_id: uuid):
# #         if 'opensearch_reports' in apps.app_configs:
# #             from individual.documents import IndividualDocument

# #             individuals = Individual.objects.filter(individualdatasource__upload=upload_id)
# #             if not individuals:
# #                 return

# #             IndividualDocument().update(individuals, 'index')

# #     @staticmethod
# #     def process_chunk(
# #         chunk,
# #         properties,
# #         unique_validations,
# #         loc_name_code_district_ids_from_db,
# #         user_allowed_loc_ids,
# #         duplicate_village_name_code_tuples,
# #     ):
# #         validated_dataframe = []
# #         check_location = 'location_name' in chunk.columns

# #         for _, row in chunk.iterrows():
# #             field_validation = {'row': row.to_dict(), 'validations': {}}
# #             for field, field_properties in properties.items():

# #                 # Validation Calculation
# #                 if "validationCalculation" in field_properties and field in row:
# #                     field_validation['validations'][field] = IndividualImportService._handle_validation_calculation(row, field, field_properties)

# #                 # Uniqueness Check
# #                 if "uniqueness" in field_properties and field in row:
# #                     field_validation['validations'][f'{field}_uniqueness'] = IndividualImportService._handle_uniqueness(row, field, unique_validations)

# #             if 'location_name' in chunk.columns:
# #                 field_validation['validations']['location_name'] = (
# #                     IndividualImportService._validate_location(
# #                         row.location_name,
# #                         row.location_code,
# #                         loc_name_code_district_ids_from_db,
# #                         user_allowed_loc_ids,
# #                         duplicate_village_name_code_tuples,
# #                     )
# #                 )

# #             validated_dataframe.append(field_validation)

# #         return validated_dataframe

# #     def _validate_possible_individuals(self, dataframe: DataFrame, upload_id: uuid):
# #         # FIX: tolerate JSON string OR OrderedDict for ModuleConfiguration
# #         schema_dict = _safe_parse_schema(IndividualConfig.individual_schema)
# #         properties = schema_dict.get("properties", {})

# #         unique_fields = [field for field, props in properties.items() if "uniqueness" in props]
# #         unique_validations = {}
# #         if unique_fields:
# #             unique_validations = {
# #                 field: dataframe[field].duplicated(keep=False) 
# #                 for field in unique_fields
# #             }

# #         check_location = 'location_name' in dataframe.columns
# #         if check_location:
# #             # Issue a single DB query instead of per row for efficiency
# #             loc_name_code_district_ids_from_db = self._query_location_district_ids(dataframe)
# #             user_allowed_loc_ids = LocationManager().get_allowed_ids(self.user)
# #             duplicate_village_name_code_tuples = self._query_duplicate_village_name_code()
# #         else:
# #             loc_name_code_district_ids_from_db = None
# #             user_allowed_loc_ids = None
# #             duplicate_village_name_code_tuples = None

# #         # TODO: Use ProcessPoolExecutor after resolving django dependency loading issue
# #         validated_dataframe = IndividualImportService.process_chunk(
# #             dataframe,
# #             properties,
# #             unique_validations,
# #             loc_name_code_district_ids_from_db,
# #             user_allowed_loc_ids,
# #             duplicate_village_name_code_tuples,
# #         )

# #         self.save_validation_error_in_data_source_bulk(validated_dataframe)
# #         invalid_items = fetch_summary_of_broken_items(upload_id)
# #         return validated_dataframe, invalid_items

# #     @staticmethod
# #     def _query_location_district_ids(df):
# #         unique_tuples = df[['location_name', 'location_code']].drop_duplicates()
# #         query = Q()
# #         for _, row in unique_tuples.iterrows():
# #             query |= Q(name=row['location_name'], code=row['location_code'])
# #         locations = Location.objects.filter(type="V", *filter_validity()).filter(query)
# #         return {(loc.name, loc.code): loc.parent.parent.id for loc in locations}

# #     @staticmethod
# #     def _query_duplicate_village_name_code():
# #         return (
# #             Location.objects
# #             .filter(type="V", *filter_validity())
# #             .values('name', 'code')
# #             .annotate(name_count=Count('id'))
# #             .filter(name_count__gt=1)
# #             .values_list('name', 'code')
# #         )

# #     @staticmethod
# #     def _validate_location(
# #         location_name,
# #         location_code,
# #         loc_name_code_district_ids_from_db,
# #         user_allowed_loc_ids,
# #         duplicate_village_name_code_tuples
# #     ):
# #         result = {
# #             'field_name': 'location_name',
# #         }
# #         if (pd.isna(location_name) or location_name == "") and (pd.isna(location_code) or location_code == ""):
# #             result['success'] = True
# #         elif loc_name_code_district_ids_from_db is None and user_allowed_loc_ids is None:
# #             result['success'] = True
# #         elif (location_name, location_code) not in loc_name_code_district_ids_from_db:
# #             result['success'] = False
# #             result['note'] = f"Location with name '{location_name}' and code '{location_code}' is not valid. Please check the spelling against the list of locations in the system."
# #         elif (location_name, location_code) in duplicate_village_name_code_tuples:
# #             result['success'] = False
# #             result['note'] = f"Location with name '{location_name}' and code '{location_code}' is ambiguous, because there are more than one location with this name and code found in the system."
# #         elif loc_name_code_district_ids_from_db[(location_name, location_code)] not in user_allowed_loc_ids:
# #             result['success'] = False
# #             result['note'] = f"Location with name '{location_name}' and code '{location_code}' is outside the current user's location permissions."
# #         else:
# #             result['success'] = True
# #         return result

# #     @staticmethod
# #     def _handle_uniqueness(row, field, unique_validations):
# #         success = not unique_validations[field].loc[row.name]
# #         result = {
# #             "success": success,
# #             "field_name": field,
# #         }
# #         if not success:
# #             result["note"] = f"'{field}' Field value '{row[field]}' is duplicated"
# #         return result

# #     @staticmethod
# #     def _handle_validation_calculation(row, field, field_properties):
# #         validation_calculation = field_properties.get("validationCalculation", {}).get("name")
# #         if not validation_calculation:
# #             raise ValueError("Missing validation name")
# #         calculation_uuid = IndividualConfig.validation_calculation_uuid
# #         calculation = get_calculation_object(calculation_uuid)
# #         result_row = calculation.calculate_if_active_for_object(
# #             validation_calculation,
# #             calculation_uuid,
# #             field_name=field,
# #             field_value=row[field],
# #         )
# #         return result_row

# #     def _create_upload_entry(self, filename):
# #         upload = IndividualDataSourceUpload(source_name=filename, source_type='individual import')
# #         upload.save(username=self.user.login_name)
# #         return upload

# #     def _validate_dataframe(self, dataframe: pd.DataFrame):
# #         if dataframe is None:
# #             raise ValueError("Unknown error while loading import file")
# #         if dataframe.empty:
# #             raise ValueError("Import file is empty")

# #     def _load_import_file(self, import_file) -> pd.DataFrame:
# #         if import_file.content_type not in self.import_loaders:
# #             raise ValueError("Unsupported content type: {}".format(import_file.content_type))

# #         return self.import_loaders[import_file.content_type](import_file)

# #     def _save_data_source(self, dataframe: pd.DataFrame, upload: IndividualDataSourceUpload):
# #         data_source_objects = []
        
# #         for _, row in dataframe.iterrows():
# #             ds = IndividualDataSource(
# #                 upload=upload,
# #                 json_ext=json.loads(row.to_json()),
# #                 validations={},
# #                 user_created=self.user,
# #                 user_updated=self.user,
# #                 uuid=uuid.uuid4()
# #             )
# #             data_source_objects.append(ds)

# #         IndividualDataSource.objects.bulk_create(data_source_objects)

# #     def _trigger_workflow(self,
# #                           workflow: WorkflowHandler,
# #                           upload: IndividualDataSourceUpload):
# #         """
# #         Run the given workflow and, if maker–checker is enabled, create the verification task
# #         for this upload (import vs update decided by workflow name).
# #         """
# #         try:
# #             # mark as triggered to avoid races
# #             upload.status = IndividualDataSourceUpload.Status.TRIGGERED
# #             upload.save(username=self.user.login_name)

# #             result = workflow.run({
# #                 'user_uuid': str(User.objects.get(username=self.user.login_name).id),
# #                 'upload_uuid': str(upload.uuid),
# #             })

# #             # If the workflow returns an explicit failure dict, fail fast
# #             if result and isinstance(result, dict) and result.get('success') is False:
# #                 raise ValueError(result.get('message', 'Unexpected error during the workflow execution'))

# #             # ---- NEW: auto-create task for verification (maker–checker) ----
# #             try:
# #                 wf_name = (getattr(workflow, "name", "") or "").lower()
# #                 is_update_flow = "update" in wf_name

# #                 if is_update_flow and IndividualConfig.enable_maker_checker_for_individual_update:
# #                     # Update path
# #                     self.create_task_with_update_valid_items(upload.uuid)
# #                     logger.info("Created UPDATE verification task for upload %s", upload.uuid)
# #                 elif (not is_update_flow) and IndividualConfig.enable_maker_checker_for_individual_upload:
# #                     # Import path
# #                     self.create_task_with_importing_valid_items(upload.uuid)
# #                     logger.info("Created IMPORT verification task for upload %s", upload.uuid)
# #                 else:
# #                     logger.debug("Maker–checker disabled for this flow (%s); no verification task created.", wf_name)
# #             except Exception:
# #                 logger.exception("Creating verification task failed for upload %s", upload.uuid)

# #         except ValueError as e:
# #             upload.status = IndividualDataSourceUpload.Status.FAIL
# #             upload.error = {'workflow': str(e)}
# #             upload.save(username=self.user.login_name)
# #             return upload

# #     def save_validation_error_in_data_source_bulk(self, validated_dataframe):
# #         data_sources_to_update = []

# #         for field_validation in validated_dataframe:
# #             row = field_validation['row']
# #             error_fields = []

# #             for key, value in field_validation['validations'].items():
# #                 if not value.get('success', False):
# #                     error_fields.append({
# #                         "field_name": value.get('field_name'),
# #                         "note": value.get('note')
# #                     })

# #             data_sources_to_update.append(
# #                 IndividualDataSource(
# #                     id=row['id'],
# #                     validations={'validation_errors': error_fields}
# #                 )
# #             )

# #         if data_sources_to_update:
# #             IndividualDataSource.objects.bulk_update(data_sources_to_update, ['validations'])

# #     def create_task_with_importing_valid_items(self, upload_id: uuid):
# #         if IndividualConfig.enable_maker_checker_for_individual_upload:
# #             IndividualTaskCreatorService(self.user) \
# #                 .create_task_with_importing_valid_items(upload_id)
# #         else:
# #             record = IndividualDataUploadRecords.objects.get(
# #                 data_upload_id=upload_id,
# #                 is_deleted=False
# #             )
# #             from individual.signals.on_validation_import_valid_items import IndividualItemsImportTaskCompletionEvent
# #             IndividualItemsImportTaskCompletionEvent(
# #                 IndividualConfig.validation_import_valid_items_workflow,
# #                 record,
# #                 record.data_upload.id,
# #                 self.user
# #             ).run_workflow()

# #     def create_task_with_update_valid_items(self, upload_id: uuid):
# #         # Resolve automatically if maker-checker not enabled
# #         if IndividualConfig.enable_maker_checker_for_individual_update:
# #             IndividualTaskCreatorService(self.user) \
# #                 .create_task_with_update_valid_items(upload_id)
# #         else:
# #             record = IndividualDataUploadRecords.objects.get(
# #                 data_upload_id=upload_id,
# #                 is_deleted=False
# #             )
# #             from individual.signals.on_validation_import_valid_items import IndividualItemsUploadTaskCompletionEvent
# #             IndividualItemsUploadTaskCompletionEvent(
# #                 IndividualConfig.validation_upload_valid_items_workflow,
# #                 record,
# #                 record.data_upload.id,
# #                 self.user
# #             ).run_workflow()

# #     # ----------------------------------------------------------------------
# #     # NEW helper: link Individuals to Groups for a given upload (explicitly call from workflow)
# #     # ----------------------------------------------------------------------
# #     @transaction.atomic
# #     def link_groups_for_upload_uuid(self, upload_id: uuid.UUID) -> dict:
# #         """
# #         For each IndividualDataSource in this upload that has an Individual and a group_code:
# #           - create/find Group(code=group_code)
# #           - ensure GroupIndividual exists
# #           - role HEAD if json_ext.individual_role_code == '1'
# #           - recipient_type PRIMARY if json_ext.hhrep == json_ext.individual_role_code
# #           - refresh Group.json_ext
# #         """
# #         alignment = GroupAndGroupIndividualAlignmentService(self.user)
# #         sources = (
# #             IndividualDataSource.objects
# #             .filter(upload_id=upload_id, is_deleted=False)
# #             .exclude(individual_id__isnull=True)
# #             .values('id', 'individual_id', 'json_ext')
# #         )

# #         created_groups = 0
# #         touched_groups = set()
# #         created_links = 0
# #         updated_links = 0

# #         for src in sources:
# #             je = src.get('json_ext') or {}
# #             group_code = (je.get('group_code') or "").strip()
# #             if not group_code:
# #                 continue

# #             role_code = str(je.get('individual_role_code') or je.get('relationship_to_head') or "").strip()
# #             hhrep_code = str(je.get('hhrep') or "").strip()

# #             group = Group.objects.filter(code=group_code, is_deleted=False).first()
# #             if not group:
# #                 group = Group(code=group_code, json_ext={})
# #                 group.save(user=self.user)
# #                 created_groups += 1
# #             touched_groups.add(group.id)

# #             individual_id = src['individual_id']
# #             gi = GroupIndividual.objects.filter(group=group, individual_id=individual_id, is_deleted=False).first()

# #             desired_role = GroupIndividual.Role.HEAD if role_code == "1" else None
# #             desired_recipient = GroupIndividual.RecipientType.PRIMARY if (hhrep_code and hhrep_code == role_code) else None

# #             if not gi:
# #                 gi = GroupIndividual(group=group, individual_id=individual_id)
# #                 gi.role = desired_role
# #                 gi.recipient_type = desired_recipient
# #                 gi.save(user=self.user)
# #                 created_links += 1
# #             else:
# #                 changed = False
# #                 if gi.role != desired_role:
# #                     gi.role = desired_role
# #                     changed = True
# #                 if gi.recipient_type != desired_recipient:
# #                     gi.recipient_type = desired_recipient
# #                     changed = True
# #                 if changed:
# #                     gi.save(user=self.user)
# #                     updated_links += 1

# #         for gid in touched_groups:
# #             g = Group.objects.get(id=gid)
# #             alignment.update_json_ext_for_group(g)

# #         return {
# #             "created_groups": created_groups,
# #             "groups_touched": len(touched_groups),
# #             "created_links": created_links,
# #             "updated_links": updated_links,
# #         }


# # class IndividualTaskCreatorService:

# #     def __init__(self, user):
# #         self.user = user

# #     def create_task_with_importing_valid_items(self, upload_id: uuid):
# #         self._create_task(upload_id, IndividualConfig.validation_import_valid_items)

# #     def create_task_with_update_valid_items(self, upload_id: uuid):
# #         self._create_task(upload_id, IndividualConfig.validation_upload_valid_items)

# #     @register_service_signal('individual.update_task')
# #     @transaction.atomic()
# #     def _create_task(self, upload_id, business_event):
# #         from tasks_management.services import TaskService
# #         from tasks_management.apps import TasksManagementConfig
# #         from tasks_management.models import Task
# #         upload_record = IndividualDataUploadRecords.objects.get(
# #             data_upload_id=upload_id,
# #             is_deleted=False
# #         )
# #         json_ext = {
# #             'source_name': upload_record.data_upload.source_name,
# #             'workflow': upload_record.workflow,
# #             'percentage_of_invalid_items': self.__calculate_percentage_of_invalid_items(upload_id),
# #             'data_upload_id': str(upload_id),
# #             'group_aggregation_column':
# #                 upload_record.json_ext.get('group_aggregation_column')
# #                 if isinstance(upload_record.json_ext, dict)
# #                 else None,
# #         }
# #         TaskService(self.user).create({
# #             'source': 'import_valid_items',
# #             'entity': upload_record,
# #             'status': Task.Status.RECEIVED,
# #             'executor_action_event': TasksManagementConfig.default_executor_event,
# #             'business_event': business_event,
# #             'json_ext': json_ext
# #         })

# #         data_upload = upload_record.data_upload
# #         data_upload.status = IndividualDataSourceUpload.Status.WAITING_FOR_VERIFICATION
# #         data_upload.save(user=self.user)

# #     def __calculate_percentage_of_invalid_items(self, upload_id):
# #         number_of_valid_items = len(fetch_summary_of_valid_items(upload_id))
# #         number_of_invalid_items = len(fetch_summary_of_broken_items(upload_id))
# #         total_items = number_of_invalid_items + number_of_valid_items

# #         if total_items == 0:
# #             percentage_of_invalid_items = 0
# #         else:
# #             percentage_of_invalid_items = (number_of_invalid_items / total_items) * 100

# #         percentage_of_invalid_items = round(percentage_of_invalid_items, 2)
# #         return percentage_of_invalid_items





# # import logging
# # import json
# # import uuid
# # import pandas as pd
# # import concurrent.futures
# # import math
# # from pandas import DataFrame
# # from django.core.files.uploadedfile import InMemoryUploadedFile
# # from django.db import transaction

# # from calculation.services import get_calculation_object
# # from core import filter_validity
# # from core.custom_filters import CustomFilterWizardStorage
# # from core.models import User
# # from core.services import BaseService
# # from core.signals import register_service_signal
# # from django.apps import apps
# # from django.utils.translation import gettext as _
# # from django.db.models import Q, OuterRef, Subquery, Count
# # from individual.apps import IndividualConfig
# # from individual.models import (
# #     Individual,
# #     IndividualDataSource,
# #     GroupIndividual,
# #     Group,
# #     IndividualDataUploadRecords,
# #     IndividualDataSourceUpload
# # )
# # from individual.utils import (
# #     load_dataframe,
# #     fetch_summary_of_valid_items,
# #     fetch_summary_of_broken_items
# # )
# # from individual.validation import (
# #     IndividualValidation,
# #     IndividualDataSourceValidation,
# #     GroupIndividualValidation,
# #     GroupValidation, CrateGroupAndMoveIndividualValidation
# # )
# # from core.services.utils import check_authentication as check_authentication, output_exception, output_result_success, \
# #     model_representation
# # from location.models import Location, LocationManager
# # from tasks_management.models import Task
# # from tasks_management.services import UpdateCheckerLogicServiceMixin, CreateCheckerLogicServiceMixin, \
# #     crud_business_data_builder, DeleteCheckerLogicServiceMixin
# # from workflow.systems.base import WorkflowHandler

# # logger = logging.getLogger(__name__)


# # class IndividualService(BaseService, UpdateCheckerLogicServiceMixin, DeleteCheckerLogicServiceMixin):
# #     @register_service_signal('individual_service.create')
# #     def create(self, obj_data):
# #         return super().create(obj_data)

# #     def create_update_task(self, obj_data):
# #         self._update_json_ext(obj_data)
# #         return super().create_update_task(obj_data)

# #     @register_service_signal('individual_service.update')
# #     def update(self, obj_data):
# #         self._update_json_ext(obj_data)
# #         return super().update(obj_data)

# #     @register_service_signal('individual_service.delete')
# #     def delete(self, obj_data):
# #         return super().delete(obj_data)

# #     @register_service_signal('individual_service.undo_delete')
# #     @check_authentication
# #     def undo_delete(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 self.validation_class.validate_undo_delete(obj_data)
# #                 obj_ = self.OBJECT_TYPE.objects.filter(id=obj_data['id']).first()
# #                 obj_.is_deleted = False
# #                 obj_.save(user=self.user)
# #                 return {
# #                     "success": True,
# #                     "message": "Ok",
# #                     "detail": "Undo Delete",
# #                 }
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="undo_delete", exception=exc)

# #     @register_service_signal('individual_service.select_individuals_to_benefit_plan')
# #     def select_individuals_to_benefit_plan(self, custom_filters, benefit_plan_id, status, user):
# #         individual_query = Individual.objects.filter(is_deleted=False)
# #         subquery = GroupIndividual.objects.filter(
# #             individual=OuterRef('pk')
# #         ).exclude(
# #             is_deleted=True
# #         ).values('individual')
# #         individual_query_with_filters = CustomFilterWizardStorage.build_custom_filters_queryset(
# #             "individual",
# #             "Individual",
# #             custom_filters,
# #             individual_query,
# #         )
# #         individual_query_with_filters = individual_query_with_filters.filter(~Q(pk__in=Subquery(subquery))).distinct()
# #         if benefit_plan_id:
# #             individuals_assigned_to_selected_programme = individual_query_with_filters. \
# #                 filter(is_deleted=False, beneficiary__benefit_plan_id=benefit_plan_id)
# #             individuals_not_assigned_to_selected_programme = individual_query_with_filters.exclude(
# #                 id__in=individuals_assigned_to_selected_programme.values_list('id', flat=True)
# #             )
# #             output = {
# #                 "individuals_assigned_to_selected_programme": individuals_assigned_to_selected_programme,
# #                 "individuals_not_assigned_to_selected_programme": individuals_not_assigned_to_selected_programme,
# #                 "individual_query_with_filters": individual_query_with_filters,
# #                 "benefit_plan_id": benefit_plan_id,
# #                 "status": status,
# #                 "user": user,
# #             }
# #             return output
# #         return None

# #     @register_service_signal('individual_service.create_accept_enrolment_task')
# #     def create_accept_enrolment_task(self, individual_queryset, benefit_plan_id):
# #         pass

# #     def _update_json_ext(self, obj_data):
# #         if not obj_data or 'json_ext' not in obj_data or 'location_id' not in obj_data:
# #             return

# #         json_ext = obj_data['json_ext']
# #         if not json_ext:
# #             return

# #         location_id = obj_data['location_id']
# #         if location_id:
# #             location = Location.objects.get(id=location_id)
# #             json_ext['location_str'] = str(location)
# #         else:
# #             json_ext['location_str'] = None

# #         obj_data['json_ext'] = json_ext

# #     OBJECT_TYPE = Individual

# #     def __init__(self, user, validation_class=IndividualValidation):
# #         super().__init__(user, validation_class)


# # class IndividualDataSourceService(BaseService):
# #     @register_service_signal('individual_data_source_service.create')
# #     def create(self, obj_data):
# #         return super().create(obj_data)

# #     @register_service_signal('individual_data_source_service.update')
# #     def update(self, obj_data):
# #         return super().update(obj_data)

# #     @register_service_signal('individual_data_source_service.delete')
# #     def delete(self, obj_data):
# #         return super().delete(obj_data)

# #     OBJECT_TYPE = IndividualDataSource

# #     def __init__(self, user, validation_class=IndividualDataSourceValidation):
# #         super().__init__(user, validation_class)


# # class GroupService(
# #     BaseService,
# #     CreateCheckerLogicServiceMixin,
# #     UpdateCheckerLogicServiceMixin,
# #     DeleteCheckerLogicServiceMixin
# # ):
# #     OBJECT_TYPE = Group

# #     def __init__(self, user, validation_class=GroupValidation):
# #         super().__init__(user, validation_class)

# #     @check_authentication
# #     @register_service_signal('group_service.create')
# #     def create(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 individuals_data = obj_data.pop('individuals_data', None)
# #                 result = super().create(obj_data)
# #                 group_id = result.get('data', {}).get('id')

# #                 if not group_id:
# #                     return result

# #                 if individuals_data:
# #                     individual_ids = [data["individual_id"] for data in individuals_data]
# #                     self._update_group_json_ext(group_id, individual_ids)
# #                     for data in individuals_data:
# #                         obj_data = {
# #                             'group_id': group_id,
# #                             'individual_id': data.get("individual_id"),
# #                             'role': data.get("role"),
# #                             'recipient_type': data.get("recipient_type")
# #                         }
# #                         service = GroupIndividualService(self.user)
# #                         service.create(obj_data)
# #                 return result
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

# #     @check_authentication
# #     @register_service_signal('group_service.update')
# #     def update(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 individuals_data = obj_data.pop('individuals_data', None)
# #                 result = super().update(obj_data)

# #                 if not individuals_data:
# #                     return result

# #                 group_id = obj_data['id']
# #                 assigned_individuals_ids = \
# #                     GroupIndividual.objects.filter(group_id=group_id).values_list('individual_id', flat=True)

# #                 service = GroupIndividualService(self.user)
# #                 individual_ids = [data['individual_id'] for data in individuals_data]
# #                 group = self._update_group_json_ext(group_id, individual_ids)

# #                 for individual_id in assigned_individuals_ids:
# #                     if str(individual_id) not in individual_ids:
# #                         group_individual = GroupIndividual.objects.get(group_id=group_id, individual_id=individual_id)
# #                         service.delete({'id': group_individual.id})

# #                 for data in individuals_data:
# #                     if uuid.UUID(data["individual_id"]) not in assigned_individuals_ids:
# #                         obj_data = {
# #                             'group_id': group_id,
# #                             'individual_id': data.get("individual_id"),
# #                             'role': data.get("role"),
# #                             'recipient_type': data.get("recipient_type")
# #                         }
# #                         service.create(obj_data)

# #                 dict_repr = model_representation(group)
# #                 return output_result_success(dict_representation=dict_repr)
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

# #     @register_service_signal('group_service.delete')
# #     def delete(self, obj_data):
# #         # if there ever was a requirement to undo group delete, remember to use members from json_ext, you will avoid
# #         # adding individuals that had been deleted from the group before group deletion
# #         with transaction.atomic():
# #             group_id = obj_data.get('id')
# #             group_individuals = GroupIndividual.objects.filter(group_id=group_id)
# #             for group_individual in group_individuals:
# #                 # cant use .delete() on query since it will completely remove instances from db instead of marking
# #                 # them as isDeleted
# #                 group_individual.delete(user=self.user)
# #             return super().delete(obj_data)

# #     @transaction.atomic
# #     def _update_group_json_ext(self, group_id, individual_ids):
# #         # it makes sure GroupIndividual .save() won't add each individual separately to group json_ext
# #         # because their ids will be already there
# #         group = Group.objects.get(id=group_id)
# #         group_members = {
# #             str(individual.id): f"{individual.first_name} {individual.last_name}"
# #             for individual in Individual.objects.filter(id__in=individual_ids)
# #         }
# #         group.json_ext["members"] = group_members
# #         group.save(user=self.user)
# #         return group

# #     @register_service_signal('group_service.select_groups_to_benefit_plan')
# #     def select_groups_to_benefit_plan(self, custom_filters, benefit_plan_id, status, user):
# #         group_query = Group.objects.filter(is_deleted=False)
# #         # criteria will be based on head of the group
# #         group_query_with_filters = CustomFilterWizardStorage.build_custom_filters_queryset(
# #             "individual",
# #             "Group",
# #             custom_filters,
# #             group_query,
# #         )
# #         if benefit_plan_id:
# #             groups_assigned_to_selected_programme = group_query_with_filters. \
# #                 filter(is_deleted=False, groupbeneficiary__benefit_plan_id=benefit_plan_id)
# #             groups_not_assigned_to_selected_programme = group_query_with_filters.exclude(
# #                 id__in=groups_assigned_to_selected_programme.values_list('id', flat=True)
# #             )
# #             output = {
# #                 "groups_assigned_to_selected_programme": groups_assigned_to_selected_programme,
# #                 "groups_not_assigned_to_selected_programme": groups_not_assigned_to_selected_programme,
# #                 "group_query_with_filters": group_query_with_filters,
# #                 "benefit_plan_id": benefit_plan_id,
# #                 "status": status,
# #                 "user": user,
# #             }
# #             return output
# #         return None


# # class CreateGroupAndMoveIndividualService(CreateCheckerLogicServiceMixin):
# #     OBJECT_TYPE = Group

# #     def __init__(self, user, validation_class=CrateGroupAndMoveIndividualValidation):
# #         self.user = user
# #         self.validation_class = validation_class

# #     @check_authentication
# #     @register_service_signal('create_group_and_move_individual.create')
# #     def create(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 self.validation_class.validate_create_group_and_move_individual(self.user, **obj_data)
# #                 group_individual_id = obj_data.pop('group_individual_id')
# #                 group = GroupService(self.user).create(obj_data)
# #                 # return group if it has errors
# #                 if not group['data']:
# #                     return group
# #                 group_individual = GroupIndividual.objects.filter(id=group_individual_id).first()
# #                 group_id = group['data']['id']
# #                 service = GroupIndividualService(self.user)
# #                 service.update({
# #                     'group_id': group_id, "id": group_individual_id, "role": group_individual.role
# #                 })
# #                 group_and_individuals_message = {**group, 'detail': group_individual_id}
# #                 return group_and_individuals_message
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

# #     def _business_data_serializer(self, data):
# #         def serialize(key, value):
# #             if key == 'group_individual_id':
# #                 group_individual = GroupIndividual.objects.get(id=value)
# #                 return f'{group_individual.individual.first_name} {group_individual.individual.last_name}'
# #             return value

# #         serialized_data = crud_business_data_builder(data, serialize)
# #         # TODO change to group code
# #         serialized_data['incoming_data']["id"] = 'NEW_GROUP'
# #         return serialized_data


# # class GroupIndividualService(BaseService, UpdateCheckerLogicServiceMixin):
# #     OBJECT_TYPE = GroupIndividual

# #     def __init__(self, user, validation_class=GroupIndividualValidation):
# #         super().__init__(user, validation_class)

# #     @register_service_signal('groupindividual_service.create')
# #     def create(self, obj_data):
# #         return super().create(obj_data)

# #     @check_authentication
# #     @register_service_signal('groupindividual_service.update')
# #     def update(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 group_individual_id = obj_data.get('id')
# #                 incoming_group_id = obj_data.get('group_id')
# #                 group_individual = GroupIndividual.objects.filter(id=group_individual_id, is_deleted=False).first()
# #                 if not group_individual:
# #                     raise ValueError(f"no GroupIndividual found with this id {group_individual_id}")

# #                 if str(group_individual.group.id) == str(incoming_group_id):
# #                     return super().update(obj_data)

# #                 obj_data.pop('id', None)
# #                 obj_data.pop('recipient_type', None)
# #                 obj_data.pop('role', None)
# #                 result = self.create(obj_data)
# #                 self.delete({'id': group_individual_id})
# #                 return result
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

# #     @register_service_signal('groupindividual_service.delete')
# #     def delete(self, obj_data):
# #         return super().delete(obj_data)

# #     def _business_data_serializer(self, data):
# #         def serialize(key, value):
# #             if key == 'id':
# #                 group_individual = GroupIndividual.objects.get(id=value)
# #                 return f'{group_individual.individual.first_name} {group_individual.individual.last_name}'
# #             if key == 'group_id':
# #                 group = Group.objects.get(id=value)
# #                 return group.code
# #             return value

# #         serialized_data = crud_business_data_builder(data, serialize)
# #         return serialized_data


# # class GroupAndGroupIndividualAlignmentService:
# #     """
# #         Service used in overridden .save() of GroupIndividual model.
# #     """

# #     def __init__(self, user):
# #         self.user = user

# #     def handle_head_change(self, group_individual_id, role, group_id):
# #         """
# #             Method used for making sure that during head change, the old one is set to default role.
# #         """
# #         if role == GroupIndividual.Role.HEAD:
# #             self._change_head(group_individual_id, group_id)

# #     def handle_primary_recipient_change(self, group_individual_id, recipient_type, group_id):
# #         """
# #             Method used for making sure that during primary recipient change, the old one is set to default role.
# #         """
# #         if recipient_type == GroupIndividual.RecipientType.PRIMARY:
# #             self._change_primary(group_individual_id, group_id)

# #     def update_json_ext_for_group(self, group):
# #         """
# #         This method ensures that json_ext of a group is up-to-date with its roles and members.
# #         """
# #         group_individuals = GroupIndividual.objects.filter(group_id=group.id, is_deleted=False)
# #         head = group_individuals.filter(role=GroupIndividual.Role.HEAD).first()
# #         primary = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.PRIMARY).first()
# #         secondary = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.SECONDARY).first()

# #         group_members = {
# #             str(individual.individual.id): f"{individual.individual.first_name} {individual.individual.last_name}"
# #             for individual in group_individuals
# #         }

# #         head_str = f'{head.individual.first_name} {head.individual.last_name}' if head else None
# #         head_id = str(head.individual.id) if head else None
# #         head_json_ext = head.individual.json_ext if head and head.individual.json_ext else {}

# #         primary_str = f'{primary.individual.first_name} {primary.individual.last_name}' if primary else None
# #         primary_id = str(primary.individual.id) if primary else None

# #         secondary_str = f'{secondary.individual.first_name} {secondary.individual.last_name}' if secondary else None
# #         secondary_id = str(secondary.individual.id) if secondary else None

# #         changes_to_save = {}
# #         json_ext_minus_keys = {k: v for k, v in group.json_ext.items() if k not in [
# #             "members", "head", "head_id", "primary_recipient",
# #             "primary_recipient_id", "secondary_recipient", "secondary_recipient_id"
# #         ]}

# #         if json_ext_minus_keys != head_json_ext:
# #             all_keys = set(head_json_ext.keys()).union(json_ext_minus_keys.keys())
# #             for key in all_keys:
# #                 value = head_json_ext.get(key)
# #                 if value is None and key in group.json_ext:
# #                     del group.json_ext[key]
# #                 else:
# #                     group.json_ext[key] = value

# #         current_members = group.json_ext.get("members", {})
# #         additional_members = {k: v for k, v in group_members.items() if k not in current_members}
# #         remove_members = {k: v for k, v in current_members.items() if k not in group_members}
# #         updated_members = {**current_members, **additional_members}
# #         for member_id in remove_members:
# #             updated_members.pop(member_id, None)

# #         if current_members != updated_members:
# #             changes_to_save["members"] = updated_members

# #         if group.json_ext.get("head") != head_str:
# #             changes_to_save["head"] = head_str

# #         if group.json_ext.get("head_id") != head_id:
# #             changes_to_save["head_id"] = head_id

# #         if group.json_ext.get("primary_recipient") != primary_str:
# #             changes_to_save["primary_recipient"] = primary_str

# #         if group.json_ext.get("primary_recipient_id") != primary_id:
# #             changes_to_save["primary_recipient_id"] = primary_id

# #         if group.json_ext.get("secondary_recipient") != secondary_str:
# #             changes_to_save["secondary_recipient"] = secondary_str

# #         if group.json_ext.get("secondary_recipient_id") != secondary_id:
# #             changes_to_save["secondary_recipient_id"] = secondary_id

# #         if changes_to_save:
# #             group.json_ext.update(changes_to_save)
# #             group.save(update_fields=['json_ext'], user=self.user)

# #     def handle_assure_primary_recipient_in_group(self, group, recipient_type):
# #         """
# #             Making sure that group has a head.
# #         """
# #         if recipient_type == GroupIndividual.RecipientType.PRIMARY:
# #             return
# #         self._assure_primary_recipient_in_group(group)

# #     def ensure_location_consistent(self, group, individual, role):
# #         if group.location_id == individual.location_id:
# #             return

# #         if role == GroupIndividual.Role.HEAD and group.location_id is None:
# #             group.location_id = individual.location_id
# #             group.save(user=self.user)
# #         else:
# #             individual.location_id = group.location_id
# #             individual.save(user=self.user)


# #     def _assure_primary_recipient_in_group(self, group):
# #         group_individuals = GroupIndividual.objects.filter(group=group, is_deleted=False)
# #         primary_exists = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.PRIMARY).exists()
# #         head_exists = group_individuals.filter(role=GroupIndividual.Role.HEAD).exists()

# #         if primary_exists:
# #             return

# #         new_primary = group_individuals.first()

# #         if not new_primary:
# #             return

# #         new_primary.recipient_type = GroupIndividual.RecipientType.PRIMARY
# #         if not head_exists:
# #             new_primary.role = GroupIndividual.Role.HEAD
# #         new_primary.save(user=self.user)

# #     def _change_head(self, group_individual_id, group_id):
# #         heads_queryset = GroupIndividual.objects.filter(group_id=group_id, role=GroupIndividual.Role.HEAD)
# #         old_head = heads_queryset.exclude(id=group_individual_id).first()

# #         if not old_head:
# #             return

# #         old_head.role = None
# #         old_head.save(user=self.user)

# #     def _change_primary(self, group_individual_id, group_id):
# #         primaries_queryset = GroupIndividual.objects.filter(
# #             group_id=group_id, recipient_type=GroupIndividual.RecipientType.PRIMARY
# #         )
# #         old_primary = primaries_queryset.exclude(id=group_individual_id).first()

# #         if not old_primary:
# #             return

# #         old_primary.recipient_type = None
# #         old_primary.save(user=self.user)


# # class IndividualImportService:
# #     import_loaders = {
# #         # CSV → read everything as text, keep blanks as blanks (no NaN)
# #         'text/csv': lambda f: pd.read_csv(f, dtype=str, keep_default_na=False),
# #         # Excel family → same behavior
# #         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': lambda f: pd.read_excel(f, dtype=str, keep_default_na=False),
# #         'application/vnd.ms-excel': lambda f: pd.read_excel(f, dtype=str, keep_default_na=False),
# #         'application/vnd.oasis.opendocument.spreadsheet': lambda f: pd.read_excel(f, dtype=str, keep_default_na=False),
# #     }

# #     def __init__(self, user):
# #         super().__init__()
# #         self.user = user

# #     @register_service_signal('individual.import_individuals')
# #     def import_individuals(self,
# #                            import_file: InMemoryUploadedFile,
# #                            workflow: WorkflowHandler,
# #                            group_aggregation_column: str):
# #         upload = self._save_sources(import_file)
# #         self._create_individual_data_upload_records(workflow, upload, group_aggregation_column)
# #         self._trigger_workflow(workflow, upload)
# #         return {'success': True, 'data': {'upload_uuid': upload.uuid}}

# #     @transaction.atomic
# #     def _save_sources(self, import_file):
# #         # Method separated as workflow execution must be independent of the atomic transaction.
# #         upload = self._create_upload_entry(import_file.name)
# #         dataframe = self._load_import_file(import_file)
# #         self._validate_dataframe(dataframe)
# #         self._save_data_source(dataframe, upload)
# #         return upload

# #     @transaction.atomic
# #     def _create_individual_data_upload_records(self, workflow, upload, group_aggregation_column):
# #         record = IndividualDataUploadRecords(
# #             data_upload=upload,
# #             workflow=workflow.name,
# #             json_ext={"group_aggregation_column": group_aggregation_column}
# #         )
# #         record.save(user=self.user)

# #     def validate_import_individuals(self, upload_id: uuid, individual_sources):
# #         dataframe = load_dataframe(individual_sources)
# #         validated_dataframe, invalid_items = self._validate_possible_individuals(
# #             dataframe,
# #             upload_id
# #         )
# #         return {'success': True, 'data': validated_dataframe, 'summary_invalid_items': invalid_items}

# #     def synchronize_data_for_reporting(self, upload_id: uuid):
# #         if 'opensearch_reports' in apps.app_configs:
# #             from individual.documents import IndividualDocument

# #             individuals = Individual.objects.filter(individualdatasource__upload=upload_id)
# #             if not individuals:
# #                 return

# #             IndividualDocument().update(individuals, 'index')

# #     @staticmethod
# #     def process_chunk(
# #         chunk,
# #         properties,
# #         unique_validations,
# #         loc_name_code_district_ids_from_db,
# #         user_allowed_loc_ids,
# #         duplicate_village_name_code_tuples,
# #     ):
# #         validated_dataframe = []
# #         check_location = 'location_name' in chunk.columns

# #         for _, row in chunk.iterrows():
# #             field_validation = {'row': row.to_dict(), 'validations': {}}
# #             for field, field_properties in properties.items():

# #                 # Validation Calculation
# #                 if "validationCalculation" in field_properties and field in row:
# #                     field_validation['validations'][field] = IndividualImportService._handle_validation_calculation(row, field, field_properties)

# #                 # Uniqueness Check
# #                 if "uniqueness" in field_properties and field in row:
# #                     field_validation['validations'][f'{field}_uniqueness'] = IndividualImportService._handle_uniqueness(row, field, unique_validations)

# #             if 'location_name' in chunk.columns:
# #                 field_validation['validations']['location_name'] = (
# #                     IndividualImportService._validate_location(
# #                         row.location_name,
# #                         row.location_code,
# #                         loc_name_code_district_ids_from_db,
# #                         user_allowed_loc_ids,
# #                         duplicate_village_name_code_tuples,
# #                     )
# #                 )

# #             validated_dataframe.append(field_validation)

# #         return validated_dataframe

# #     def _validate_possible_individuals(self, dataframe: DataFrame, upload_id: uuid):
# #         schema_dict = json.loads(IndividualConfig.individual_schema)
# #         properties = schema_dict.get("properties", {})

# #         unique_fields = [field for field, props in properties.items() if "uniqueness" in props]
# #         unique_validations = {}
# #         if unique_fields:
# #             unique_validations = {
# #                 field: dataframe[field].duplicated(keep=False) 
# #                 for field in unique_fields
# #             }

# #         check_location = 'location_name' in dataframe.columns
# #         if check_location:
# #             # Issue a single DB query instead of per row for efficiency
# #             loc_name_code_district_ids_from_db = self._query_location_district_ids(dataframe)
# #             user_allowed_loc_ids = LocationManager().get_allowed_ids(self.user)
# #             duplicate_village_name_code_tuples = self._query_duplicate_village_name_code()
# #         else:
# #             loc_name_code_district_ids_from_db = None
# #             user_allowed_loc_ids = None
# #             duplicate_village_name_code_tuples = None

# #         # TODO: Use ProcessPoolExecutor after resolving django dependency loading issue
# #         validated_dataframe = IndividualImportService.process_chunk(
# #             dataframe,
# #             properties,
# #             unique_validations,
# #             loc_name_code_district_ids_from_db,
# #             user_allowed_loc_ids,
# #             duplicate_village_name_code_tuples,
# #         )

# #         self.save_validation_error_in_data_source_bulk(validated_dataframe)
# #         invalid_items = fetch_summary_of_broken_items(upload_id)
# #         return validated_dataframe, invalid_items

# #     @staticmethod
# #     def _query_location_district_ids(df):
# #         # Normalize codes and drop empties before querying
# #         if 'location_name' not in df.columns or 'location_code' not in df.columns:
# #             return {}

# #         tuples_df = (
# #             df[['location_name', 'location_code']]
# #             .drop_duplicates()
# #             .assign(
# #                 location_code=lambda d: d['location_code']
# #                     .astype(str).str.strip()
# #                     .str.replace(r'\.0$', '', regex=True)
# #                     .str.replace(r'[^0-9]', '', regex=True)
# #             )
# #         )
# #         # Keep only rows with both name and a non-empty code
# #         tuples_df = tuples_df[
# #             tuples_df['location_name'].astype(str).str.strip() != ''
# #         ]
# #         tuples_df = tuples_df[
# #             tuples_df['location_code'].astype(str).str.len() > 0
# #         ]
# #         # Pad valid codes to 9 digits
# #         tuples_df['location_code'] = tuples_df['location_code'].str.zfill(9)

# #         q = Q()
# #         for name, code in tuples_df.itertuples(index=False, name=None):
# #             q |= Q(name=name, code=code)

# #         if not q.children:
# #             return {}

# #         locations = Location.objects.filter(type="V", *filter_validity()).filter(q)
# #         return {(loc.name, loc.code): loc.parent.parent.id for loc in locations}

# #     @staticmethod
# #     def _query_duplicate_village_name_code():
# #         return (
# #             Location.objects
# #             .filter(type="V", *filter_validity())
# #             .values('name', 'code')
# #             .annotate(name_count=Count('id'))
# #             .filter(name_count__gt=1)
# #             .values_list('name', 'code')
# #         )

# #     @staticmethod
# #     def _validate_location(
# #         location_name,
# #         location_code,
# #         loc_name_code_district_ids_from_db,
# #         user_allowed_loc_ids,
# #         duplicate_village_name_code_tuples
# #     ):
# #         result = {
# #             'field_name': 'location_name',
# #         }

# #         # Normalize code safely (string-only, keep blanks)
# #         code = '' if pd.isna(location_code) else str(location_code).strip()
# #         # strip excel-related artifacts and non-digits
# #         code = (
# #             code.replace('.0', '')  # quick path for very common artifact
# #         )
# #         code = ''.join(ch for ch in code if ch.isdigit())
# #         if code:
# #             code = code.zfill(9)

# #         if (pd.isna(location_name) or str(location_name).strip() == "") and code == "":
# #             result['success'] = True
# #         elif loc_name_code_district_ids_from_db is None and user_allowed_loc_ids is None:
# #             result['success'] = True
# #         elif (location_name, code) not in loc_name_code_district_ids_from_db:
# #             result['success'] = False
# #             result['note'] = (
# #                 f"Location with name '{location_name}' and code '{code}' is not valid. "
# #                 "Please check the spelling against the list of locations in the system."
# #             )
# #         elif (location_name, code) in duplicate_village_name_code_tuples:
# #             result['success'] = False
# #             result['note'] = (
# #                 f"Location with name '{location_name}' and code '{code}' is ambiguous, "
# #                 "because there is more than one matching location in the system."
# #             )
# #         elif loc_name_code_district_ids_from_db[(location_name, code)] not in user_allowed_loc_ids:
# #             result['success'] = False
# #             result['note'] = (
# #                 f"Location with name '{location_name}' and code '{code}' is outside the current user's location permissions."
# #             )
# #         else:
# #             result['success'] = True
# #         return result

# #     # ---------- helpers for import ----------

# #     @staticmethod
# #     def _normalize_code_series(series: pd.Series, width: int) -> pd.Series:
# #         """
# #         Normalize a code column coming from CSV/Excel:
# #           - treat as str; strip
# #           - drop trailing '.0' (Excel)
# #           - keep digits only
# #           - pad to width for non-empty values (blank stays blank)
# #         """
# #         s = series.astype(str).str.strip()
# #         s = s.str.replace(r'\.0$', '', regex=True)
# #         s = s.str.replace(r'[^0-9]', '', regex=True)
# #         mask = s.str.len() > 0
# #         s.loc[mask] = s.loc[mask].str.zfill(width)
# #         return s

# #     def _create_upload_entry(self, filename):
# #         upload = IndividualDataSourceUpload(source_name=filename, source_type='individual import')
# #         upload.save(username=self.user.login_name)
# #         return upload

# #     def _validate_dataframe(self, dataframe: pd.DataFrame):
# #         if dataframe is None:
# #             raise ValueError("Unknown error while loading import file")
# #         if dataframe.empty:
# #             raise ValueError("Import file is empty")

# #     def _load_import_file(self, import_file) -> pd.DataFrame:
# #         if import_file.content_type not in self.import_loaders:
# #             raise ValueError("Unsupported content type: {}".format(import_file.content_type))

# #         df = self.import_loaders[import_file.content_type](import_file)

# #         # Normalize common code columns if present
# #         if 'location_code' in df.columns:
# #             df['location_code'] = self._normalize_code_series(df['location_code'], 9)
# #         if 'ward_code' in df.columns:
# #             df['ward_code'] = self._normalize_code_series(df['ward_code'], 6)
# #         if 'district_code' in df.columns:
# #             df['district_code'] = self._normalize_code_series(df['district_code'], 4)
# #         if 'region_code' in df.columns:
# #             df['region_code'] = self._normalize_code_series(df['region_code'], 2)

# #         return df

# #     def _save_data_source(self, dataframe: pd.DataFrame, upload: IndividualDataSourceUpload):
# #         data_source_objects = []
        
# #         for _, row in dataframe.iterrows():
# #             ds = IndividualDataSource(
# #                 upload=upload,
# #                 json_ext=json.loads(row.to_json()),
# #                 validations={},
# #                 user_created=self.user,
# #                 user_updated=self.user,
# #                 uuid=uuid.uuid4()
# #             )
# #             data_source_objects.append(ds)

# #         IndividualDataSource.objects.bulk_create(data_source_objects)

# #     def _trigger_workflow(self,
# #                           workflow: WorkflowHandler,
# #                           upload: IndividualDataSourceUpload):
# #         try:
# #             # Before the run in order to avoid racing conditions
# #             upload.status = IndividualDataSourceUpload.Status.TRIGGERED
# #             upload.save(username=self.user.login_name)

# #             result = workflow.run({
# #                 # Core user UUID required
# #                 'user_uuid': str(User.objects.get(username=self.user.login_name).id),
# #                 'upload_uuid': str(upload.uuid),
# #             })

# #             # Conditions are safety measure for workflows. Usually handles like PythonHandler or LightningHandler
# #             #  should follow this pattern but return type is not determined in workflow.run abstract.
# #             if result and isinstance(result, dict) and result.get('success') is False:
# #                 raise ValueError(result.get('message', 'Unexpected error during the workflow execution'))
# #         except Exception as e:
# #             upload.status = IndividualDataSourceUpload.Status.FAIL
# #             upload.error = {'workflow': repr(e)}
# #             upload.save(username=self.user.login_name)
# #             return upload

# #     def save_validation_error_in_data_source_bulk(self, validated_dataframe):
# #         data_sources_to_update = []

# #         for field_validation in validated_dataframe:
# #             row = field_validation['row']
# #             error_fields = []

# #             for key, value in field_validation['validations'].items():
# #                 if not value.get('success', False):
# #                     error_fields.append({
# #                         "field_name": value.get('field_name'),
# #                         "note": value.get('note')
# #                     })

# #             data_sources_to_update.append(
# #                 IndividualDataSource(
# #                     id=row['id'],
# #                     validations={'validation_errors': error_fields}
# #                 )
# #             )

# #         if data_sources_to_update:
# #             IndividualDataSource.objects.bulk_update(data_sources_to_update, ['validations'])

# #     def create_task_with_importing_valid_items(self, upload_id: uuid):
# #         if IndividualConfig.enable_maker_checker_for_individual_upload:
# #             IndividualTaskCreatorService(self.user) \
# #                 .create_task_with_importing_valid_items(upload_id)
# #         else:
# #             record = IndividualDataUploadRecords.objects.get(
# #                 data_upload_id=upload_id,
# #                 is_deleted=False
# #             )
# #             from individual.signals.on_validation_import_valid_items import IndividualItemsImportTaskCompletionEvent
# #             IndividualItemsImportTaskCompletionEvent(
# #                 IndividualConfig.validation_import_valid_items_workflow,
# #                 record,
# #                 record.data_upload.id,
# #                 self.user
# #             ).run_workflow()

# #     def create_task_with_update_valid_items(self, upload_id: uuid):
# #         # Resolve automatically if maker-checker not enabled
# #         if IndividualConfig.enable_maker_checker_for_individual_update:
# #             IndividualTaskCreatorService(self.user) \
# #                 .create_task_with_update_valid_items(upload_id)
# #         else:
# #             record = IndividualDataUploadRecords.objects.get(
# #                 data_upload_id=upload_id,
# #                 is_deleted=False
# #             )
# #             from individual.signals.on_validation_import_valid_items import IndividualItemsUploadTaskCompletionEvent
# #             IndividualItemsUploadTaskCompletionEvent(
# #                 IndividualConfig.validation_upload_valid_items_workflow,
# #                 record,
# #                 record.data_upload.id,
# #                 self.user
# #             ).run_workflow()

# #     # ---- convenience wrappers to call linking from workflows (optional) ----
# #     def link_groups_for_upload_uuid(self, upload_uuid: str) -> dict:
# #         return GroupImportLinker(self.user).link_upload_uuid(upload_uuid)

# #     def link_groups_for_upload_id(self, upload_id) -> dict:
# #         return GroupImportLinker(self.user).link_upload_id(upload_id)


# # class IndividualTaskCreatorService:

# #     def __init__(self, user):
# #         self.user = user

# #     def create_task_with_importing_valid_items(self, upload_id: uuid):
# #         self._create_task(upload_id, IndividualConfig.validation_import_valid_items)

# #     def create_task_with_update_valid_items(self, upload_id: uuid):
# #         self._create_task(upload_id, IndividualConfig.validation_upload_valid_items)

# #     @register_service_signal('individual.update_task')
# #     @transaction.atomic()
# #     def _create_task(self, upload_id, business_event):
# #         from tasks_management.services import TaskService
# #         from tasks_management.apps import TasksManagementConfig
# #         from tasks_management.models import Task
# #         upload_record = IndividualDataUploadRecords.objects.get(
# #             data_upload_id=upload_id,
# #             is_deleted=False
# #         )
# #         json_ext = {
# #             'source_name': upload_record.data_upload.source_name,
# #             'workflow': upload_record.workflow,
# #             'percentage_of_invalid_items': self.__calculate_percentage_of_invalid_items(upload_id),
# #             'data_upload_id': str(upload_id),
# #             'group_aggregation_column':
# #                 upload_record.json_ext.get('group_aggregation_column')
# #                 if isinstance(upload_record.json_ext, dict)
# #                 else None,
# #         }
# #         TaskService(self.user).create({
# #             'source': 'import_valid_items',
# #             'entity': upload_record,
# #             'status': Task.Status.RECEIVED,
# #             'executor_action_event': TasksManagementConfig.default_executor_event,
# #             'business_event': business_event,
# #             'json_ext': json_ext
# #         })

# #         data_upload = upload_record.data_upload
# #         data_upload.status = IndividualDataSourceUpload.Status.WAITING_FOR_VERIFICATION
# #         data_upload.save(user=self.user)

# #     def __calculate_percentage_of_invalid_items(self, upload_id):
# #         number_of_valid_items = len(fetch_summary_of_valid_items(upload_id))
# #         number_of_invalid_items = len(fetch_summary_of_broken_items(upload_id))
# #         total_items = number_of_invalid_items + number_of_valid_items

# #         if total_items == 0:
# #             percentage_of_invalid_items = 0
# #         else:
# #             percentage_of_invalid_items = (number_of_invalid_items / total_items) * 100

# #         percentage_of_invalid_items = round(percentage_of_invalid_items, 2)
# #         return percentage_of_invalid_items


# # # ============================
# # # Group wiring post-import
# # # ============================

# # class GroupImportLinker:
# #     """
# #     Wire imported Individuals into Groups (households) by group_code, using existing services.

# #     Representative detection (no fallback rules):
# #       - member-level: individual_role == 'hhrep'  -> that member is PRIMARY
# #       - group-level:  hhrep == <member external_id> -> that member is PRIMARY

# #     No automatic promotion of head/first member.
# #     """

# #     ROLE_ALIASES_HEAD = {"head", "household_head", "hh_head", "hod"}
# #     ROLE_ALIASES_PRIMARY = {"primary", "representative", "house_representative", "rep", "hhrep"}
# #     ROLE_ALIASES_SECONDARY = {"secondary", "alternate", "alt"}

# #     # group-level pointer fields (value should be the representative member's external_id)
# #     HHREP_POINTER_FIELDS = ("hhrep", "hhrep_external_id", "rep_external_id")

# #     def __init__(self, user):
# #         self.user = user
# #         self._align = GroupAndGroupIndividualAlignmentService(user)

# #     def _role_from_text(self, txt: str):
# #         txt = (txt or "").strip().lower()
# #         role = None
# #         recipient_type = None
# #         if txt in self.ROLE_ALIASES_HEAD:
# #             role = GroupIndividual.Role.HEAD
# #             recipient_type = GroupIndividual.RecipientType.PRIMARY  # many programs treat head as rep; survey may override
# #         elif txt in self.ROLE_ALIASES_PRIMARY:
# #             recipient_type = GroupIndividual.RecipientType.PRIMARY
# #         elif txt in self.ROLE_ALIASES_SECONDARY:
# #             recipient_type = GroupIndividual.RecipientType.SECONDARY
# #         return role, recipient_type

# #     def _extract_group_hhrep_pointer(self, people_for_group):
# #         """
# #         Scan members' json_ext to find a group-level 'hhrep' pointer value.
# #         We treat this value as an external_id to match a member.
# #         """
# #         for person, _role, _rcpt in people_for_group:
# #             j = person.json_ext or {}
# #             for f in self.HHREP_POINTER_FIELDS:
# #                 v = j.get(f)
# #                 if v:
# #                     return str(v).strip()
# #         return None

# #     def _set_primary_by_hhrep(self, group: Group, hhrep_external_id: str) -> bool:
# #         if not hhrep_external_id:
# #             return False
# #         gis = GroupIndividual.objects.filter(group=group, is_deleted=False).select_related("individual")
# #         target = None
# #         for gi in gis:
# #             ext = (gi.individual.json_ext or {}).get("external_id")
# #             if ext and str(ext).strip() == hhrep_external_id:
# #                 target = gi
# #                 break
# #         if not target:
# #             return False
# #         if target.recipient_type != GroupIndividual.RecipientType.PRIMARY:
# #             target.recipient_type = GroupIndividual.RecipientType.PRIMARY
# #             target.save(user=self.user)
# #         return True

# #     def link_upload_uuid(self, upload_uuid: str) -> dict:
# #         upload = IndividualDataSourceUpload.objects.filter(uuid=upload_uuid, is_deleted=False).first()
# #         if not upload:
# #             logger.warning("GroupImportLinker: upload %s not found", upload_uuid)
# #             return {"success": False, "message": "upload not found"}
# #         return self.link_upload_id(upload.id)

# #     @transaction.atomic
# #     def link_upload_id(self, upload_id) -> dict:
# #         # All individuals created by this upload
# #         people = (
# #             Individual.objects
# #             .filter(individualdatasource__upload=upload_id, is_deleted=False)
# #             .distinct()
# #         )
# #         if not people:
# #             return {"success": True, "processed": 0}

# #         # Group by code and prepare role data
# #         by_code = {}  # code -> list[(person, role, recipient)]
# #         hhrep_pointer = {}  # code -> external_id (string)
# #         for p in people:
# #             j = p.json_ext or {}
# #             gc = j.get("group_code")
# #             if not gc:
# #                 continue
# #             role, recipient = self._role_from_text(j.get("individual_role"))
# #             by_code.setdefault(gc, []).append((p, role, recipient))

# #         # compute group-level hhrep pointer once per group
# #         for code, members in by_code.items():
# #             hhrep_pointer[code] = self._extract_group_hhrep_pointer(members)

# #         processed = 0

# #         for code, members in by_code.items():
# #             group = Group.objects.filter(code=code, is_deleted=False).first()
# #             if not group:
# #                 # New household: create with all members at once
# #                 individuals_data = []
# #                 for person, role, recipient in members:
# #                     individuals_data.append({
# #                         "individual_id": str(person.id),
# #                         "role": role,
# #                         "recipient_type": recipient,
# #                     })

# #                 # Initialize group location from head (if any) else first member; survey enforces head presence anyway
# #                 head = next((p for p, r, _ in members if r == GroupIndividual.Role.HEAD), None)
# #                 loc_id = head.location_id if head and head.location_id else (members[0][0].location_id if members and members[0][0].location_id else None)

# #                 gs = GroupService(self.user)
# #                 create_payload = {
# #                     "code": code,
# #                     "location_id": loc_id,
# #                     "json_ext": {},
# #                     "individuals_data": individuals_data,
# #                 }
# #                 result = gs.create(create_payload)
# #                 gid = result.get("data", {}).get("id") if isinstance(result, dict) else None
# #                 if not gid:
# #                     logger.warning("GroupImportLinker: failed to create group %s, result=%s", code, result)
# #                     continue
# #                 group = Group.objects.get(id=gid)

# #                 # If a group-level pointer exists, set PRIMARY accordingly (no fallback)
# #                 self._set_primary_by_hhrep(group, hhrep_pointer.get(code))

# #                 # Keep json_ext aligned (members/head/primary/secondary snapshot)
# #                 self._align.update_json_ext_for_group(group)
# #                 processed += len(members)
# #                 continue

# #             # Existing group: append each member (no roster replacement)
# #             gi_service = GroupIndividualService(self.user)
# #             for person, role, recipient in members:
# #                 # ensure location consistency for heads / members
# #                 self._align.ensure_location_consistent(group, person, role)

# #                 existing = GroupIndividual.objects.filter(group=group, individual=person, is_deleted=False).first()
# #                 if existing:
# #                     changed = False
# #                     update_data = {"id": existing.id}
# #                     if role is not None and existing.role != role:
# #                         update_data["role"] = role
# #                         changed = True
# #                     if recipient is not None and existing.recipient_type != recipient:
# #                         update_data["recipient_type"] = recipient
# #                         changed = True
# #                     if changed:
# #                         gi_service.update(update_data)
# #                     processed += 1
# #                     continue

# #                 payload = {
# #                     "group_id": str(group.id),
# #                     "individual_id": str(person.id),
# #                 }
# #                 if role is not None:
# #                     payload["role"] = role
# #                 if recipient is not None:
# #                     payload["recipient_type"] = recipient
# #                 gi_service.create(payload)
# #                 processed += 1

# #             # Apply group-level pointer if provided (no fallback)
# #             self._set_primary_by_hhrep(group, hhrep_pointer.get(code))

# #             # Finally, update json_ext snapshot
# #             self._align.update_json_ext_for_group(group)

# #         return {"success": True, "processed": processed}

# ####################################


# # import logging
# # import json
# # import uuid
# # import pandas as pd
# # import concurrent.futures
# # import math
# # from pandas import DataFrame
# # from django.core.files.uploadedfile import InMemoryUploadedFile
# # from django.db import transaction

# # from calculation.services import get_calculation_object
# # from core import filter_validity
# # from core.custom_filters import CustomFilterWizardStorage
# # from core.models import User
# # from core.services import BaseService
# # from core.signals import register_service_signal
# # from django.apps import apps
# # from django.utils.translation import gettext as _
# # from django.db.models import Q, OuterRef, Subquery, Count
# # from individual.apps import IndividualConfig
# # from individual.models import (
# #     Individual,
# #     IndividualDataSource,
# #     GroupIndividual,
# #     Group,
# #     IndividualDataUploadRecords,
# #     IndividualDataSourceUpload
# # )
# # from individual.utils import (
# #     load_dataframe,
# #     fetch_summary_of_valid_items,
# #     fetch_summary_of_broken_items
# # )
# # from individual.validation import (
# #     IndividualValidation,
# #     IndividualDataSourceValidation,
# #     GroupIndividualValidation,
# #     GroupValidation, CrateGroupAndMoveIndividualValidation
# # )
# # from core.services.utils import check_authentication as check_authentication, output_exception, output_result_success, \
# #     model_representation
# # from location.models import Location, LocationManager
# # from tasks_management.models import Task
# # from tasks_management.services import UpdateCheckerLogicServiceMixin, CreateCheckerLogicServiceMixin, \
# #     crud_business_data_builder, DeleteCheckerLogicServiceMixin
# # from workflow.systems.base import WorkflowHandler

# # logger = logging.getLogger(__name__)


# # class IndividualService(BaseService, UpdateCheckerLogicServiceMixin, DeleteCheckerLogicServiceMixin):
# #     @register_service_signal('individual_service.create')
# #     def create(self, obj_data):
# #         return super().create(obj_data)

# #     def create_update_task(self, obj_data):
# #         self._update_json_ext(obj_data)
# #         return super().create_update_task(obj_data)

# #     @register_service_signal('individual_service.update')
# #     def update(self, obj_data):
# #         self._update_json_ext(obj_data)
# #         return super().update(obj_data)

# #     @register_service_signal('individual_service.delete')
# #     def delete(self, obj_data):
# #         return super().delete(obj_data)

# #     @register_service_signal('individual_service.undo_delete')
# #     @check_authentication
# #     def undo_delete(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 self.validation_class.validate_undo_delete(obj_data)
# #                 obj_ = self.OBJECT_TYPE.objects.filter(id=obj_data['id']).first()
# #                 obj_.is_deleted = False
# #                 obj_.save(user=self.user)
# #                 return {
# #                     "success": True,
# #                     "message": "Ok",
# #                     "detail": "Undo Delete",
# #                 }
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="undo_delete", exception=exc)

# #     @register_service_signal('individual_service.select_individuals_to_benefit_plan')
# #     def select_individuals_to_benefit_plan(self, custom_filters, benefit_plan_id, status, user):
# #         individual_query = Individual.objects.filter(is_deleted=False)
# #         subquery = GroupIndividual.objects.filter(
# #             individual=OuterRef('pk')
# #         ).exclude(
# #             is_deleted=True
# #         ).values('individual')
# #         individual_query_with_filters = CustomFilterWizardStorage.build_custom_filters_queryset(
# #             "individual",
# #             "Individual",
# #             custom_filters,
# #             individual_query,
# #         )
# #         individual_query_with_filters = individual_query_with_filters.filter(~Q(pk__in=Subquery(subquery))).distinct()
# #         if benefit_plan_id:
# #             individuals_assigned_to_selected_programme = individual_query_with_filters. \
# #                 filter(is_deleted=False, beneficiary__benefit_plan_id=benefit_plan_id)
# #             individuals_not_assigned_to_selected_programme = individual_query_with_filters.exclude(
# #                 id__in=individuals_assigned_to_selected_programme.values_list('id', flat=True)
# #             )
# #             output = {
# #                 "individuals_assigned_to_selected_programme": individuals_assigned_to_selected_programme,
# #                 "individuals_not_assigned_to_selected_programme": individuals_not_assigned_to_selected_programme,
# #                 "individual_query_with_filters": individual_query_with_filters,
# #                 "benefit_plan_id": benefit_plan_id,
# #                 "status": status,
# #                 "user": user,
# #             }
# #             return output
# #         return None

# #     @register_service_signal('individual_service.create_accept_enrolment_task')
# #     def create_accept_enrolment_task(self, individual_queryset, benefit_plan_id):
# #         pass

# #     def _update_json_ext(self, obj_data):
# #         if not obj_data or 'json_ext' not in obj_data or 'location_id' not in obj_data:
# #             return

# #         json_ext = obj_data['json_ext']
# #         if not json_ext:
# #             return

# #         location_id = obj_data['location_id']
# #         if location_id:
# #             location = Location.objects.get(id=location_id)
# #             json_ext['location_str'] = str(location)
# #         else:
# #             json_ext['location_str'] = None

# #         obj_data['json_ext'] = json_ext

# #     OBJECT_TYPE = Individual

# #     def __init__(self, user, validation_class=IndividualValidation):
# #         super().__init__(user, validation_class)


# # class IndividualDataSourceService(BaseService):
# #     @register_service_signal('individual_data_source_service.create')
# #     def create(self, obj_data):
# #         return super().create(obj_data)

# #     @register_service_signal('individual_data_source_service.update')
# #     def update(self, obj_data):
# #         return super().update(obj_data)

# #     @register_service_signal('individual_data_source_service.delete')
# #     def delete(self, obj_data):
# #         return super().delete(obj_data)

# #     OBJECT_TYPE = IndividualDataSource

# #     def __init__(self, user, validation_class=IndividualDataSourceValidation):
# #         super().__init__(user, validation_class)


# # class GroupService(
# #     BaseService,
# #     CreateCheckerLogicServiceMixin,
# #     UpdateCheckerLogicServiceMixin,
# #     DeleteCheckerLogicServiceMixin
# # ):
# #     OBJECT_TYPE = Group

# #     def __init__(self, user, validation_class=GroupValidation):
# #         super().__init__(user, validation_class)

# #     @check_authentication
# #     @register_service_signal('group_service.create')
# #     def create(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 individuals_data = obj_data.pop('individuals_data', None)
# #                 result = super().create(obj_data)
# #                 group_id = result.get('data', {}).get('id')

# #                 if not group_id:
# #                     return result

# #                 if individuals_data:
# #                     individual_ids = [data["individual_id"] for data in individuals_data]
# #                     self._update_group_json_ext(group_id, individual_ids)
# #                     for data in individuals_data:
# #                         obj_data = {
# #                             'group_id': group_id,
# #                             'individual_id': data.get("individual_id"),
# #                             'role': data.get("role"),
# #                             'recipient_type': data.get("recipient_type")
# #                         }
# #                         service = GroupIndividualService(self.user)
# #                         service.create(obj_data)
# #                 return result
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

# #     @check_authentication
# #     @register_service_signal('group_service.update')
# #     def update(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 individuals_data = obj_data.pop('individuals_data', None)
# #                 result = super().update(obj_data)

# #                 if not individuals_data:
# #                     return result

# #                 group_id = obj_data['id']
# #                 assigned_individuals_ids = \
# #                     GroupIndividual.objects.filter(group_id=group_id).values_list('individual_id', flat=True)

# #                 service = GroupIndividualService(self.user)
# #                 individual_ids = [data['individual_id'] for data in individuals_data]
# #                 group = self._update_group_json_ext(group_id, individual_ids)

# #                 for individual_id in assigned_individuals_ids:
# #                     if str(individual_id) not in individual_ids:
# #                         group_individual = GroupIndividual.objects.get(group_id=group_id, individual_id=individual_id)
# #                         service.delete({'id': group_individual.id})

# #                 for data in individuals_data:
# #                     if uuid.UUID(data["individual_id"]) not in assigned_individuals_ids:
# #                         obj_data = {
# #                             'group_id': group_id,
# #                             'individual_id': data.get("individual_id"),
# #                             'role': data.get("role"),
# #                             'recipient_type': data.get("recipient_type")
# #                         }
# #                         service.create(obj_data)

# #                 dict_repr = model_representation(group)
# #                 return output_result_success(dict_representation=dict_repr)
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

# #     @register_service_signal('group_service.delete')
# #     def delete(self, obj_data):
# #         # if there ever was a requirement to undo group delete, remember to use members from json_ext, you will avoid
# #         # adding individuals that had been deleted from the group before group deletion
# #         with transaction.atomic():
# #             group_id = obj_data.get('id')
# #             group_individuals = GroupIndividual.objects.filter(group_id=group_id)
# #             for group_individual in group_individuals:
# #                 # cant use .delete() on query since it will completely remove instances from db instead of marking
# #                 # them as isDeleted
# #                 group_individual.delete(user=self.user)
# #             return super().delete(obj_data)

# #     @transaction.atomic
# #     def _update_group_json_ext(self, group_id, individual_ids):
# #         # it makes sure GroupIndividual .save() won't add each individual separately to group json_ext
# #         # because their ids will be already there
# #         group = Group.objects.get(id=group_id)
# #         group_members = {
# #             str(individual.id): f"{individual.first_name} {individual.last_name}"
# #             for individual in Individual.objects.filter(id__in=individual_ids)
# #         }
# #         group.json_ext["members"] = group_members
# #         group.save(user=self.user)
# #         return group

# #     @register_service_signal('group_service.select_groups_to_benefit_plan')
# #     def select_groups_to_benefit_plan(self, custom_filters, benefit_plan_id, status, user):
# #         group_query = Group.objects.filter(is_deleted=False)
# #         # criteria will be based on head of the group
# #         group_query_with_filters = CustomFilterWizardStorage.build_custom_filters_queryset(
# #             "individual",
# #             "Group",
# #             custom_filters,
# #             group_query,
# #         )
# #         if benefit_plan_id:
# #             groups_assigned_to_selected_programme = group_query_with_filters. \
# #                 filter(is_deleted=False, groupbeneficiary__benefit_plan_id=benefit_plan_id)
# #             groups_not_assigned_to_selected_programme = group_query_with_filters.exclude(
# #                 id__in=groups_assigned_to_selected_programme.values_list('id', flat=True)
# #             )
# #             output = {
# #                 "groups_assigned_to_selected_programme": groups_assigned_to_selected_programme,
# #                 "groups_not_assigned_to_selected_programme": groups_not_assigned_to_selected_programme,
# #                 "group_query_with_filters": group_query_with_filters,
# #                 "benefit_plan_id": benefit_plan_id,
# #                 "status": status,
# #                 "user": user,
# #             }
# #             return output
# #         return None


# # class CreateGroupAndMoveIndividualService(CreateCheckerLogicServiceMixin):
# #     OBJECT_TYPE = Group

# #     def __init__(self, user, validation_class=CrateGroupAndMoveIndividualValidation):
# #         self.user = user
# #         self.validation_class = validation_class

# #     @check_authentication
# #     @register_service_signal('create_group_and_move_individual.create')
# #     def create(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 self.validation_class.validate_create_group_and_move_individual(self.user, **obj_data)
# #                 group_individual_id = obj_data.pop('group_individual_id')
# #                 group = GroupService(self.user).create(obj_data)
# #                 # return group if it has errors
# #                 if not group['data']:
# #                     return group
# #                 group_individual = GroupIndividual.objects.filter(id=group_individual_id).first()
# #                 group_id = group['data']['id']
# #                 service = GroupIndividualService(self.user)
# #                 service.update({
# #                     'group_id': group_id, "id": group_individual_id, "role": group_individual.role
# #                 })
# #                 group_and_individuals_message = {**group, 'detail': group_individual_id}
# #                 return group_and_individuals_message
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

# #     def _business_data_serializer(self, data):
# #         def serialize(key, value):
# #             if key == 'group_individual_id':
# #                 group_individual = GroupIndividual.objects.get(id=value)
# #                 return f'{group_individual.individual.first_name} {group_individual.individual.last_name}'
# #             return value

# #         serialized_data = crud_business_data_builder(data, serialize)
# #         # TODO change to group code
# #         serialized_data['incoming_data']["id"] = 'NEW_GROUP'
# #         return serialized_data


# # class GroupIndividualService(BaseService, UpdateCheckerLogicServiceMixin):
# #     OBJECT_TYPE = GroupIndividual

# #     def __init__(self, user, validation_class=GroupIndividualValidation):
# #         super().__init__(user, validation_class)

# #     @register_service_signal('groupindividual_service.create')
# #     def create(self, obj_data):
# #         return super().create(obj_data)

# #     @check_authentication
# #     @register_service_signal('groupindividual_service.update')
# #     def update(self, obj_data):
# #         try:
# #             with transaction.atomic():
# #                 group_individual_id = obj_data.get('id')
# #                 incoming_group_id = obj_data.get('group_id')
# #                 group_individual = GroupIndividual.objects.filter(id=group_individual_id, is_deleted=False).first()
# #                 if not group_individual:
# #                     raise ValueError(f"no GroupIndividual found with this id {group_individual_id}")

# #                 if str(group_individual.group.id) == str(incoming_group_id):
# #                     return super().update(obj_data)

# #                 obj_data.pop('id', None)
# #                 obj_data.pop('recipient_type', None)
# #                 obj_data.pop('role', None)
# #                 result = self.create(obj_data)
# #                 self.delete({'id': group_individual_id})
# #                 return result
# #         except Exception as exc:
# #             return output_exception(model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc)

# #     @register_service_signal('groupindividual_service.delete')
# #     def delete(self, obj_data):
# #         return super().delete(obj_data)

# #     def _business_data_serializer(self, data):
# #         def serialize(key, value):
# #             if key == 'id':
# #                 group_individual = GroupIndividual.objects.get(id=value)
# #                 return f'{group_individual.individual.first_name} {group_individual.individual.last_name}'
# #             if key == 'group_id':
# #                 group = Group.objects.get(id=value)
# #                 return group.code
# #             return value

# #         serialized_data = crud_business_data_builder(data, serialize)
# #         return serialized_data


# # class GroupAndGroupIndividualAlignmentService:
# #     """
# #         Service used in overridden .save() of GroupIndividual model.
# #     """

# #     def __init__(self, user):
# #         self.user = user

# #     def handle_head_change(self, group_individual_id, role, group_id):
# #         """
# #             Method used for making sure that during head change, the old one is set to default role.
# #         """
# #         if role == GroupIndividual.Role.HEAD:
# #             self._change_head(group_individual_id, group_id)

# #     def handle_primary_recipient_change(self, group_individual_id, recipient_type, group_id):
# #         """
# #             Method used for making sure that during primary recipient change, the old one is set to default role.
# #         """
# #         if recipient_type == GroupIndividual.RecipientType.PRIMARY:
# #             self._change_primary(group_individual_id, group_id)

# #     def update_json_ext_for_group(self, group):
# #         """
# #         This method ensures that json_ext of a group is up-to-date with its roles and members.
# #         """
# #         group_individuals = GroupIndividual.objects.filter(group_id=group.id, is_deleted=False)
# #         head = group_individuals.filter(role=GroupIndividual.Role.HEAD).first()
# #         primary = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.PRIMARY).first()
# #         secondary = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.SECONDARY).first()

# #         group_members = {
# #             str(individual.individual.id): f"{individual.individual.first_name} {individual.individual.last_name}"
# #             for individual in group_individuals
# #         }

# #         head_str = f'{head.individual.first_name} {head.individual.last_name}' if head else None
# #         head_id = str(head.individual.id) if head else None
# #         head_json_ext = head.individual.json_ext if head and head.individual.json_ext else {}

# #         primary_str = f'{primary.individual.first_name} {primary.individual.last_name}' if primary else None
# #         primary_id = str(primary.individual.id) if primary else None

# #         secondary_str = f'{secondary.individual.first_name} {secondary.individual.last_name}' if secondary else None
# #         secondary_id = str(secondary.individual.id) if secondary else None

# #         changes_to_save = {}
# #         json_ext_minus_keys = {k: v for k, v in group.json_ext.items() if k not in [
# #             "members", "head", "head_id", "primary_recipient",
# #             "primary_recipient_id", "secondary_recipient", "secondary_recipient_id"
# #         ]}

# #         if json_ext_minus_keys != head_json_ext:
# #             all_keys = set(head_json_ext.keys()).union(json_ext_minus_keys.keys())
# #             for key in all_keys:
# #                 value = head_json_ext.get(key)
# #                 if value is None and key in group.json_ext:
# #                     del group.json_ext[key]
# #                 else:
# #                     group.json_ext[key] = value

# #         current_members = group.json_ext.get("members", {})
# #         additional_members = {k: v for k, v in group_members.items() if k not in current_members}
# #         remove_members = {k: v for k, v in current_members.items() if k not in group_members}
# #         updated_members = {**current_members, **additional_members}
# #         for member_id in remove_members:
# #             updated_members.pop(member_id, None)

# #         if current_members != updated_members:
# #             changes_to_save["members"] = updated_members

# #         if group.json_ext.get("head") != head_str:
# #             changes_to_save["head"] = head_str

# #         if group.json_ext.get("head_id") != head_id:
# #             changes_to_save["head_id"] = head_id

# #         if group.json_ext.get("primary_recipient") != primary_str:
# #             changes_to_save["primary_recipient"] = primary_str

# #         if group.json_ext.get("primary_recipient_id") != primary_id:
# #             changes_to_save["primary_recipient_id"] = primary_id

# #         if group.json_ext.get("secondary_recipient") != secondary_str:
# #             changes_to_save["secondary_recipient"] = secondary_str

# #         if group.json_ext.get("secondary_recipient_id") != secondary_id:
# #             changes_to_save["secondary_recipient_id"] = secondary_id

# #         if changes_to_save:
# #             group.json_ext.update(changes_to_save)
# #             group.save(update_fields=['json_ext'], user=self.user)

# #     def handle_assure_primary_recipient_in_group(self, group, recipient_type):
# #         """
# #             Making sure that group has a head.
# #         """
# #         if recipient_type == GroupIndividual.RecipientType.PRIMARY:
# #             return
# #         self._assure_primary_recipient_in_group(group)

# #     def ensure_location_consistent(self, group, individual, role):
# #         if group.location_id == individual.location_id:
# #             return

# #         if role == GroupIndividual.Role.HEAD and group.location_id is None:
# #             group.location_id = individual.location_id
# #             group.save(user=self.user)
# #         else:
# #             individual.location_id = group.location_id
# #             individual.save(user=self.user)


# #     def _assure_primary_recipient_in_group(self, group):
# #         group_individuals = GroupIndividual.objects.filter(group=group, is_deleted=False)
# #         primary_exists = group_individuals.filter(recipient_type=GroupIndividual.RecipientType.PRIMARY).exists()
# #         head_exists = group_individuals.filter(role=GroupIndividual.Role.HEAD).exists()

# #         if primary_exists:
# #             return

# #         new_primary = group_individuals.first()

# #         if not new_primary:
# #             return

# #         new_primary.recipient_type = GroupIndividual.RecipientType.PRIMARY
# #         if not head_exists:
# #             new_primary.role = GroupIndividual.Role.HEAD
# #         new_primary.save(user=self.user)

# #     def _change_head(self, group_individual_id, group_id):
# #         heads_queryset = GroupIndividual.objects.filter(group_id=group_id, role=GroupIndividual.Role.HEAD)
# #         old_head = heads_queryset.exclude(id=group_individual_id).first()

# #         if not old_head:
# #             return

# #         old_head.role = None
# #         old_head.save(user=self.user)

# #     def _change_primary(self, group_individual_id, group_id):
# #         primaries_queryset = GroupIndividual.objects.filter(
# #             group_id=group_id, recipient_type=GroupIndividual.RecipientType.PRIMARY
# #         )
# #         old_primary = primaries_queryset.exclude(id=group_individual_id).first()

# #         if not old_primary:
# #             return

# #         old_primary.recipient_type = None
# #         old_primary.save(user=self.user)


# # class IndividualImportService:
# #     import_loaders = {
# #         # CSV → read everything as text, keep blanks as blanks (no NaN)
# #         'text/csv': lambda f: pd.read_csv(f, dtype=str, keep_default_na=False),
# #         # Excel family → same behavior
# #         'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': lambda f: pd.read_excel(f, dtype=str, keep_default_na=False),
# #         'application/vnd.ms-excel': lambda f: pd.read_excel(f, dtype=str, keep_default_na=False),
# #         'application/vnd.oasis.opendocument.spreadsheet': lambda f: pd.read_excel(f, dtype=str, keep_default_na=False),
# #     }

# #     def __init__(self, user):
# #         super().__init__()
# #         self.user = user

# #     @register_service_signal('individual.import_individuals')
# #     def import_individuals(self,
# #                            import_file: InMemoryUploadedFile,
# #                            workflow: WorkflowHandler,
# #                            group_aggregation_column: str):
# #         upload = self._save_sources(import_file)
# #         self._create_individual_data_upload_records(workflow, upload, group_aggregation_column)
# #         self._trigger_workflow(workflow, upload)
# #         return {'success': True, 'data': {'upload_uuid': upload.uuid}}

# #     @transaction.atomic
# #     def _save_sources(self, import_file):
# #         # Method separated as workflow execution must be independent of the atomic transaction.
# #         upload = self._create_upload_entry(import_file.name)
# #         dataframe = self._load_import_file(import_file)
# #         self._validate_dataframe(dataframe)
# #         self._save_data_source(dataframe, upload)
# #         return upload

# #     @transaction.atomic
# #     def _create_individual_data_upload_records(self, workflow, upload, group_aggregation_column):
# #         record = IndividualDataUploadRecords(
# #             data_upload=upload,
# #             workflow=workflow.name,
# #             json_ext={"group_aggregation_column": group_aggregation_column}
# #         )
# #         record.save(user=self.user)

# #     def validate_import_individuals(self, upload_id: uuid, individual_sources):
# #         dataframe = load_dataframe(individual_sources)
# #         validated_dataframe, invalid_items = self._validate_possible_individuals(
# #             dataframe,
# #             upload_id
# #         )
# #         return {'success': True, 'data': validated_dataframe, 'summary_invalid_items': invalid_items}

# #     def synchronize_data_for_reporting(self, upload_id: uuid):
# #         if 'opensearch_reports' in apps.app_configs:
# #             from individual.documents import IndividualDocument

# #             individuals = Individual.objects.filter(individualdatasource__upload=upload_id)
# #             if not individuals:
# #                 return

# #             IndividualDocument().update(individuals, 'index')

# #     @staticmethod
# #     def process_chunk(
# #         chunk,
# #         properties,
# #         unique_validations,
# #         loc_name_code_district_ids_from_db,
# #         user_allowed_loc_ids,
# #         duplicate_village_name_code_tuples,
# #     ):
# #         validated_dataframe = []
# #         check_location = 'location_name' in chunk.columns

# #         for _, row in chunk.iterrows():
# #             field_validation = {'row': row.to_dict(), 'validations': {}}
# #             for field, field_properties in properties.items():

# #                 # Validation Calculation
# #                 if "validationCalculation" in field_properties and field in row:
# #                     field_validation['validations'][field] = IndividualImportService._handle_validation_calculation(row, field, field_properties)

# #                 # Uniqueness Check
# #                 if "uniqueness" in field_properties and field in row:
# #                     field_validation['validations'][f'{field}_uniqueness'] = IndividualImportService._handle_uniqueness(row, field, unique_validations)

# #             if 'location_name' in chunk.columns:
# #                 field_validation['validations']['location_name'] = (
# #                     IndividualImportService._validate_location(
# #                         row.location_name,
# #                         row.location_code,
# #                         loc_name_code_district_ids_from_db,
# #                         user_allowed_loc_ids,
# #                         duplicate_village_name_code_tuples,
# #                     )
# #                 )

# #             validated_dataframe.append(field_validation)

# #         return validated_dataframe

# #     def _validate_possible_individuals(self, dataframe: DataFrame, upload_id: uuid):
# #         schema_dict = json.loads(IndividualConfig.individual_schema)
# #         properties = schema_dict.get("properties", {})

# #         unique_fields = [field for field, props in properties.items() if "uniqueness" in props]
# #         unique_validations = {}
# #         if unique_fields:
# #             unique_validations = {
# #                 field: dataframe[field].duplicated(keep=False) 
# #                 for field in unique_fields
# #             }

# #         check_location = 'location_name' in dataframe.columns
# #         if check_location:
# #             # Issue a single DB query instead of per row for efficiency
# #             loc_name_code_district_ids_from_db = self._query_location_district_ids(dataframe)
# #             user_allowed_loc_ids = LocationManager().get_allowed_ids(self.user)
# #             duplicate_village_name_code_tuples = self._query_duplicate_village_name_code()
# #         else:
# #             loc_name_code_district_ids_from_db = None
# #             user_allowed_loc_ids = None
# #             duplicate_village_name_code_tuples = None

# #         # TODO: Use ProcessPoolExecutor after resolving django dependency loading issue
# #         validated_dataframe = IndividualImportService.process_chunk(
# #             dataframe,
# #             properties,
# #             unique_validations,
# #             loc_name_code_district_ids_from_db,
# #             user_allowed_loc_ids,
# #             duplicate_village_name_code_tuples,
# #         )

# #         self.save_validation_error_in_data_source_bulk(validated_dataframe)
# #         invalid_items = fetch_summary_of_broken_items(upload_id)
# #         return validated_dataframe, invalid_items

# #     @staticmethod
# #     def _query_location_district_ids(df):
# #         # Normalize codes and drop empties before querying
# #         if 'location_name' not in df.columns or 'location_code' not in df.columns:
# #             return {}

# #         tuples_df = (
# #             df[['location_name', 'location_code']]
# #             .drop_duplicates()
# #             .assign(
# #                 location_code=lambda d: d['location_code']
# #                     .astype(str).str.strip()
# #                     .str.replace(r'\.0$', '', regex=True)
# #                     .str.replace(r'[^0-9]', '', regex=True)
# #             )
# #         )
# #         # Keep only rows with both name and a non-empty code
# #         tuples_df = tuples_df[
# #             tuples_df['location_name'].astype(str).str.strip() != ''
# #         ]
# #         tuples_df = tuples_df[
# #             tuples_df['location_code'].astype(str).str.len() > 0
# #         ]
# #         # Pad valid codes to 9 digits
# #         tuples_df['location_code'] = tuples_df['location_code'].str.zfill(9)

# #         q = Q()
# #         for name, code in tuples_df.itertuples(index=False, name=None):
# #             q |= Q(name=name, code=code)

# #         if not q.children:
# #             return {}

# #         locations = Location.objects.filter(type="V", *filter_validity()).filter(q)
# #         return {(loc.name, loc.code): loc.parent.parent.id for loc in locations}

# #     @staticmethod
# #     def _query_duplicate_village_name_code():
# #         return (
# #             Location.objects
# #             .filter(type="V", *filter_validity())
# #             .values('name', 'code')
# #             .annotate(name_count=Count('id'))
# #             .filter(name_count__gt=1)
# #             .values_list('name', 'code')
# #         )

# #     @staticmethod
# #     def _validate_location(
# #         location_name,
# #         location_code,
# #         loc_name_code_district_ids_from_db,
# #         user_allowed_loc_ids,
# #         duplicate_village_name_code_tuples
# #     ):
# #         result = {
# #             'field_name': 'location_name',
# #         }

# #         # Normalize code safely (string-only, keep blanks)
# #         code = '' if pd.isna(location_code) else str(location_code).strip()
# #         # strip excel-related artifacts and non-digits
# #         code = (
# #             code.replace('.0', '')  # quick path for very common artifact
# #         )
# #         code = ''.join(ch for ch in code if ch.isdigit())
# #         if code:
# #             code = code.zfill(9)

# #         if (pd.isna(location_name) or str(location_name).strip() == "") and code == "":
# #             result['success'] = True
# #         elif loc_name_code_district_ids_from_db is None and user_allowed_loc_ids is None:
# #             result['success'] = True
# #         elif (location_name, code) not in loc_name_code_district_ids_from_db:
# #             result['success'] = False
# #             result['note'] = (
# #                 f"Location with name '{location_name}' and code '{code}' is not valid. "
# #                 "Please check the spelling against the list of locations in the system."
# #             )
# #         elif (location_name, code) in duplicate_village_name_code_tuples:
# #             result['success'] = False
# #             result['note'] = (
# #                 f"Location with name '{location_name}' and code '{code}' is ambiguous, "
# #                 "because there is more than one matching location in the system."
# #             )
# #         elif loc_name_code_district_ids_from_db[(location_name, code)] not in user_allowed_loc_ids:
# #             result['success'] = False
# #             result['note'] = (
# #                 f"Location with name '{location_name}' and code '{code}' is outside the current user's location permissions."
# #             )
# #         else:
# #             result['success'] = True
# #         return result

# #     # ---------- helpers for import ----------

# #     @staticmethod
# #     def _normalize_code_series(series: pd.Series, width: int) -> pd.Series:
# #         """
# #         Normalize a code column coming from CSV/Excel:
# #           - treat as str; strip
# #           - drop trailing '.0' (Excel)
# #           - keep digits only
# #           - pad to width for non-empty values (blank stays blank)
# #         """
# #         s = series.astype(str).str.strip()
# #         s = s.str.replace(r'\.0$', '', regex=True)
# #         s = s.str.replace(r'[^0-9]', '', regex=True)
# #         mask = s.str.len() > 0
# #         s.loc[mask] = s.loc[mask].str.zfill(width)
# #         return s
        
# #     @staticmethod
# #     def _handle_uniqueness(row, field, unique_validations):
# #         # Guard for safety if 'field' is missing from the map
# #         series = unique_validations.get(field)
# #         success = True if series is None else (not bool(series.loc[row.name]))
# #         result = {
# #             "success": success,
# #             "field_name": field,
# #         }
# #         if not success:
# #             result["note"] = f"'{field}' field value '{row[field]}' is duplicated"
# #         return result

# #     @staticmethod
# #     def _handle_validation_calculation(row, field, field_properties):
# #         validation_calculation = field_properties.get("validationCalculation", {}).get("name")
# #         if not validation_calculation:
# #             raise ValueError("Missing validation name")
# #         calculation_uuid = IndividualConfig.validation_calculation_uuid
# #         calculation = get_calculation_object(calculation_uuid)
# #         return calculation.calculate_if_active_for_object(
# #             validation_calculation,
# #             calculation_uuid,
# #             field_name=field,
# #             field_value=row.get(field),
# #         )

# #     def _create_upload_entry(self, filename):
# #         upload = IndividualDataSourceUpload(source_name=filename, source_type='individual import')
# #         upload.save(username=self.user.login_name)
# #         return upload

# #     def _validate_dataframe(self, dataframe: pd.DataFrame):
# #         if dataframe is None:
# #             raise ValueError("Unknown error while loading import file")
# #         if dataframe.empty:
# #             raise ValueError("Import file is empty")

# #     def _load_import_file(self, import_file) -> pd.DataFrame:
# #         if import_file.content_type not in self.import_loaders:
# #             raise ValueError("Unsupported content type: {}".format(import_file.content_type))

# #         df = self.import_loaders[import_file.content_type](import_file)

# #         # Normalize common code columns if present
# #         if 'location_code' in df.columns:
# #             df['location_code'] = self._normalize_code_series(df['location_code'], 9)
# #         if 'ward_code' in df.columns:
# #             df['ward_code'] = self._normalize_code_series(df['ward_code'], 6)
# #         if 'district_code' in df.columns:
# #             df['district_code'] = self._normalize_code_series(df['district_code'], 4)
# #         if 'region_code' in df.columns:
# #             df['region_code'] = self._normalize_code_series(df['region_code'], 2)

# #         return df

# #     def _save_data_source(self, dataframe: pd.DataFrame, upload: IndividualDataSourceUpload):
# #         data_source_objects = []
        
# #         for _, row in dataframe.iterrows():
# #             ds = IndividualDataSource(
# #                 upload=upload,
# #                 json_ext=json.loads(row.to_json()),
# #                 validations={},
# #                 user_created=self.user,
# #                 user_updated=self.user,
# #                 uuid=uuid.uuid4()
# #             )
# #             data_source_objects.append(ds)

# #         IndividualDataSource.objects.bulk_create(data_source_objects)

# #     def _trigger_workflow(self,
# #                           workflow: WorkflowHandler,
# #                           upload: IndividualDataSourceUpload):
# #         try:
# #             # Before the run in order to avoid racing conditions
# #             upload.status = IndividualDataSourceUpload.Status.TRIGGERED
# #             upload.save(username=self.user.login_name)

# #             result = workflow.run({
# #                 # Core user UUID required
# #                 'user_uuid': str(User.objects.get(username=self.user.login_name).id),
# #                 'upload_uuid': str(upload.uuid),
# #             })

# #             # Conditions are safety measure for workflows. Usually handles like PythonHandler or LightningHandler
# #             #  should follow this pattern but return type is not determined in workflow.run abstract.
# #             if result and isinstance(result, dict) and result.get('success') is False:
# #                 raise ValueError(result.get('message', 'Unexpected error during the workflow execution'))
# #         except ValueError as e:
# #             upload.status = IndividualDataSourceUpload.Status.FAIL
# #             upload.error = {'workflow': str(e)}
# #             upload.save(username=self.user.login_name)
# #             return upload

# #     def save_validation_error_in_data_source_bulk(self, validated_dataframe):
# #         data_sources_to_update = []

# #         for field_validation in validated_dataframe:
# #             row = field_validation['row']
# #             error_fields = []

# #             for key, value in field_validation['validations'].items():
# #                 if not value.get('success', False):
# #                     error_fields.append({
# #                         "field_name": value.get('field_name'),
# #                         "note": value.get('note')
# #                     })

# #             data_sources_to_update.append(
# #                 IndividualDataSource(
# #                     id=row['id'],
# #                     validations={'validation_errors': error_fields}
# #                 )
# #             )

# #         if data_sources_to_update:
# #             IndividualDataSource.objects.bulk_update(data_sources_to_update, ['validations'])

# #     def create_task_with_importing_valid_items(self, upload_id: uuid):
# #         if IndividualConfig.enable_maker_checker_for_individual_upload:
# #             IndividualTaskCreatorService(self.user) \
# #                 .create_task_with_importing_valid_items(upload_id)
# #         else:
# #             record = IndividualDataUploadRecords.objects.get(
# #                 data_upload_id=upload_id,
# #                 is_deleted=False
# #             )
# #             from individual.signals.on_validation_import_valid_items import IndividualItemsImportTaskCompletionEvent
# #             IndividualItemsImportTaskCompletionEvent(
# #                 IndividualConfig.validation_import_valid_items_workflow,
# #                 record,
# #                 record.data_upload.id,
# #                 self.user
# #             ).run_workflow()

# #     def create_task_with_update_valid_items(self, upload_id: uuid):
# #         # Resolve automatically if maker-checker not enabled
# #         if IndividualConfig.enable_maker_checker_for_individual_update:
# #             IndividualTaskCreatorService(self.user) \
# #                 .create_task_with_update_valid_items(upload_id)
# #         else:
# #             record = IndividualDataUploadRecords.objects.get(
# #                 data_upload_id=upload_id,
# #                 is_deleted=False
# #             )
# #             from individual.signals.on_validation_import_valid_items import IndividualItemsUploadTaskCompletionEvent
# #             IndividualItemsUploadTaskCompletionEvent(
# #                 IndividualConfig.validation_upload_valid_items_workflow,
# #                 record,
# #                 record.data_upload.id,
# #                 self.user
# #             ).run_workflow()

# # class IndividualTaskCreatorService:

# #     def __init__(self, user):
# #         self.user = user

# #     def create_task_with_importing_valid_items(self, upload_id: uuid):
# #         self._create_task(upload_id, IndividualConfig.validation_import_valid_items)

# #     def create_task_with_update_valid_items(self, upload_id: uuid):
# #         self._create_task(upload_id, IndividualConfig.validation_upload_valid_items)

# #     @register_service_signal('individual.update_task')
# #     @transaction.atomic()
# #     def _create_task(self, upload_id, business_event):
# #         from tasks_management.services import TaskService
# #         from tasks_management.apps import TasksManagementConfig
# #         from tasks_management.models import Task
# #         upload_record = IndividualDataUploadRecords.objects.get(
# #             data_upload_id=upload_id,
# #             is_deleted=False
# #         )
# #         json_ext = {
# #             'source_name': upload_record.data_upload.source_name,
# #             'workflow': upload_record.workflow,
# #             'percentage_of_invalid_items': self.__calculate_percentage_of_invalid_items(upload_id),
# #             'data_upload_id': str(upload_id),
# #             'group_aggregation_column':
# #                 upload_record.json_ext.get('group_aggregation_column')
# #                 if isinstance(upload_record.json_ext, dict)
# #                 else None,
# #         }
# #         TaskService(self.user).create({
# #             'source': 'import_valid_items',
# #             'entity': upload_record,
# #             'status': Task.Status.RECEIVED,
# #             'executor_action_event': TasksManagementConfig.default_executor_event,
# #             'business_event': business_event,
# #             'json_ext': json_ext
# #         })

# #         data_upload = upload_record.data_upload
# #         data_upload.status = IndividualDataSourceUpload.Status.WAITING_FOR_VERIFICATION
# #         data_upload.save(user=self.user)

# #     def __calculate_percentage_of_invalid_items(self, upload_id):
# #         number_of_valid_items = len(fetch_summary_of_valid_items(upload_id))
# #         number_of_invalid_items = len(fetch_summary_of_broken_items(upload_id))
# #         total_items = number_of_invalid_items + number_of_valid_items

# #         if total_items == 0:
# #             percentage_of_invalid_items = 0
# #         else:
# #             percentage_of_invalid_items = (number_of_invalid_items / total_items) * 100

# #         percentage_of_invalid_items = round(percentage_of_invalid_items, 2)
# #         return percentage_of_invalid_items
