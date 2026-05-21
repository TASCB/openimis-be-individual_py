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
    IndividualDataSourceUpload,
)
from individual.utils import (
    load_dataframe,
    fetch_summary_of_valid_items,
    fetch_summary_of_broken_items,
)
from individual.validation import (
    IndividualValidation,
    IndividualDataSourceValidation,
    GroupIndividualValidation,
    GroupValidation,
    CrateGroupAndMoveIndividualValidation,
)
from core.services.utils import (
    check_authentication as check_authentication,
    output_exception,
    output_result_success,
    model_representation,
)
from location.models import Location, LocationManager
from tasks_management.models import Task
from tasks_management.services import (
    UpdateCheckerLogicServiceMixin,
    CreateCheckerLogicServiceMixin,
    crud_business_data_builder,
    DeleteCheckerLogicServiceMixin,
)
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
            logger.warning(
                "NOOP workflow used; nothing executed. Name requested: %s", workflow
            )
            return {"success": True, "detail": "noop"}

    return _NoOp(name=str(workflow))


class IndividualService(
    BaseService, UpdateCheckerLogicServiceMixin, DeleteCheckerLogicServiceMixin
):
    @register_service_signal("individual_service.create")
    def create(self, obj_data):
        return super().create(obj_data)

    def create_update_task(self, obj_data):
        self._update_json_ext(obj_data)
        return super().create_update_task(obj_data)

    @register_service_signal("individual_service.update")
    def update(self, obj_data):
        self._update_json_ext(obj_data)
        return super().update(obj_data)

    @register_service_signal("individual_service.delete")
    def delete(self, obj_data):
        return super().delete(obj_data)

    @register_service_signal("individual_service.undo_delete")
    @check_authentication
    def undo_delete(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_undo_delete(obj_data)
                obj_ = self.OBJECT_TYPE.objects.filter(id=obj_data["id"]).first()
                obj_.is_deleted = False
                obj_.save(user=self.user)
                return {
                    "success": True,
                    "message": "Ok",
                    "detail": "Undo Delete",
                }
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__,
                method="undo_delete",
                exception=exc,
            )

    @register_service_signal("individual_service.select_individuals_to_benefit_plan")
    def select_individuals_to_benefit_plan(
        self, custom_filters, benefit_plan_id, status, user
    ):
        individual_query = Individual.objects.filter(is_deleted=False)
        subquery = (
            GroupIndividual.objects.filter(individual=OuterRef("pk"))
            .exclude(is_deleted=True)
            .values("individual")
        )
        individual_query_with_filters = (
            CustomFilterWizardStorage.build_custom_filters_queryset(
                "individual",
                "Individual",
                custom_filters,
                individual_query,
            )
        )
        individual_query_with_filters = individual_query_with_filters.filter(
            ~Q(pk__in=Subquery(subquery))
        ).distinct()
        if benefit_plan_id:
            individuals_assigned_to_selected_programme = (
                individual_query_with_filters.filter(
                    is_deleted=False, beneficiary__benefit_plan_id=benefit_plan_id
                )
            )
            individuals_not_assigned_to_selected_programme = (
                individual_query_with_filters.exclude(
                    id__in=individuals_assigned_to_selected_programme.values_list(
                        "id", flat=True
                    )
                )
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

    @register_service_signal("individual_service.create_accept_enrolment_task")
    def create_accept_enrolment_task(self, individual_queryset, benefit_plan_id):
        pass

    def _update_json_ext(self, obj_data):
        if not obj_data or "json_ext" not in obj_data or "location_id" not in obj_data:
            return

        json_ext = obj_data["json_ext"]
        if not json_ext:
            return

        location_id = obj_data["location_id"]
        if location_id:
            location = Location.objects.get(id=location_id)
            json_ext["location_str"] = str(location)
        else:
            json_ext["location_str"] = None

        obj_data["json_ext"] = json_ext

    OBJECT_TYPE = Individual

    def __init__(self, user, validation_class=IndividualValidation):
        super().__init__(user, validation_class)


class IndividualDataSourceService(BaseService):
    @register_service_signal("individual_data_source_service.create")
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal("individual_data_source_service.update")
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal("individual_data_source_service.delete")
    def delete(self, obj_data):
        return super().delete(obj_data)

    OBJECT_TYPE = IndividualDataSource

    def __init__(self, user, validation_class=IndividualDataSourceValidation):
        super().__init__(user, validation_class)


class GroupService(
    BaseService,
    CreateCheckerLogicServiceMixin,
    UpdateCheckerLogicServiceMixin,
    DeleteCheckerLogicServiceMixin,
):
    OBJECT_TYPE = Group

    def __init__(self, user, validation_class=GroupValidation):
        super().__init__(user, validation_class)

    @check_authentication
    @register_service_signal("group_service.create")
    def create(self, obj_data):
        try:
            with transaction.atomic():
                individuals_data = obj_data.pop("individuals_data", None)
                result = super().create(obj_data)
                group_id = result.get("data", {}).get("id")

                if not group_id:
                    return result

                if individuals_data:
                    individual_ids = [
                        data["individual_id"] for data in individuals_data
                    ]
                    self._update_group_json_ext(group_id, individual_ids)
                    for data in individuals_data:
                        obj_data = {
                            "group_id": group_id,
                            "individual_id": data.get("individual_id"),
                            "role": data.get("role"),
                            "recipient_type": data.get("recipient_type"),
                        }
                        service = GroupIndividualService(self.user)
                        service.create(obj_data)
                return result
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc
            )

    @check_authentication
    @register_service_signal("group_service.update")
    def update(self, obj_data):
        try:
            with transaction.atomic():
                individuals_data = obj_data.pop("individuals_data", None)
                result = super().update(obj_data)

                if not individuals_data:
                    return result

                group_id = obj_data["id"]
                assigned_individuals_ids = GroupIndividual.objects.filter(
                    group_id=group_id
                ).values_list("individual_id", flat=True)

                service = GroupIndividualService(self.user)
                individual_ids = [data["individual_id"] for data in individuals_data]
                group = self._update_group_json_ext(group_id, individual_ids)

                for individual_id in assigned_individuals_ids:
                    if str(individual_id) not in individual_ids:
                        group_individual = GroupIndividual.objects.get(
                            group_id=group_id, individual_id=individual_id
                        )
                        service.delete({"id": group_individual.id})

                for data in individuals_data:
                    if uuid.UUID(data["individual_id"]) not in assigned_individuals_ids:
                        obj_data = {
                            "group_id": group_id,
                            "individual_id": data.get("individual_id"),
                            "role": data.get("role"),
                            "recipient_type": data.get("recipient_type"),
                        }
                        service.create(obj_data)

                dict_repr = model_representation(group)
                return output_result_success(dict_representation=dict_repr)
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc
            )

    @register_service_signal("group_service.delete")
    def delete(self, obj_data):
        # if there ever was a requirement to undo group delete, remember to use members from json_ext, you will avoid
        # adding individuals that had been deleted from the group before group deletion
        with transaction.atomic():
            group_id = obj_data.get("id")
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

    @register_service_signal("group_service.select_groups_to_benefit_plan")
    def select_groups_to_benefit_plan(
        self, custom_filters, benefit_plan_id, status, user
    ):
        group_query = Group.objects.filter(is_deleted=False)
        # criteria will be based on head of the group
        group_query_with_filters = (
            CustomFilterWizardStorage.build_custom_filters_queryset(
                "individual",
                "Group",
                custom_filters,
                group_query,
            )
        )
        if benefit_plan_id:
            groups_assigned_to_selected_programme = group_query_with_filters.filter(
                is_deleted=False, groupbeneficiary__benefit_plan_id=benefit_plan_id
            )
            groups_not_assigned_to_selected_programme = (
                group_query_with_filters.exclude(
                    id__in=groups_assigned_to_selected_programme.values_list(
                        "id", flat=True
                    )
                )
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
    @register_service_signal("create_group_and_move_individual.create")
    def create(self, obj_data):
        try:
            with transaction.atomic():
                self.validation_class.validate_create_group_and_move_individual(
                    self.user, **obj_data
                )
                group_individual_id = obj_data.pop("group_individual_id")
                group = GroupService(self.user).create(obj_data)
                # return group if it has errors
                if not group["data"]:
                    return group
                group_individual = GroupIndividual.objects.filter(
                    id=group_individual_id
                ).first()
                group_id = group["data"]["id"]
                service = GroupIndividualService(self.user)
                service.update(
                    {
                        "group_id": group_id,
                        "id": group_individual_id,
                        "role": group_individual.role,
                    }
                )
                group_and_individuals_message = {**group, "detail": group_individual_id}
                return group_and_individuals_message
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc
            )

    def _business_data_serializer(self, data):
        def serialize(key, value):
            if key == "group_individual_id":
                group_individual = GroupIndividual.objects.get(id=value)
                return f"{group_individual.individual.first_name} {group_individual.individual.last_name}"
            return value

        serialized_data = crud_business_data_builder(data, serialize)
        # TODO change to group code
        serialized_data["incoming_data"]["id"] = "NEW_GROUP"
        return serialized_data


class GroupIndividualService(BaseService, UpdateCheckerLogicServiceMixin):
    OBJECT_TYPE = GroupIndividual

    def __init__(self, user, validation_class=GroupIndividualValidation):
        super().__init__(user, validation_class)

    @register_service_signal("groupindividual_service.create")
    def create(self, obj_data):
        return super().create(obj_data)

    @check_authentication
    @register_service_signal("groupindividual_service.update")
    def update(self, obj_data):
        try:
            with transaction.atomic():
                group_individual_id = obj_data.get("id")
                incoming_group_id = obj_data.get("group_id")
                group_individual = GroupIndividual.objects.filter(
                    id=group_individual_id, is_deleted=False
                ).first()
                if not group_individual:
                    raise ValueError(
                        f"no GroupIndividual found with this id {group_individual_id}"
                    )

                if str(group_individual.group.id) == str(incoming_group_id):
                    return super().update(obj_data)

                obj_data.pop("id", None)
                obj_data.pop("recipient_type", None)
                obj_data.pop("role", None)
                result = self.create(obj_data)
                self.delete({"id": group_individual_id})
                return result
        except Exception as exc:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__, method="update", exception=exc
            )

    @register_service_signal("groupindividual_service.delete")
    def delete(self, obj_data):
        return super().delete(obj_data)

    def _business_data_serializer(self, data):
        def serialize(key, value):
            if key == "id":
                group_individual = GroupIndividual.objects.get(id=value)
                return f"{group_individual.individual.first_name} {group_individual.individual.last_name}"
            if key == "group_id":
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

    def handle_primary_recipient_change(
        self, group_individual_id, recipient_type, group_id
    ):
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
        group_individuals = GroupIndividual.objects.filter(
            group_id=group.id, is_deleted=False
        )
        head = group_individuals.filter(role=GroupIndividual.Role.HEAD).first()
        primary = group_individuals.filter(
            recipient_type=GroupIndividual.RecipientType.PRIMARY
        ).first()
        secondary = group_individuals.filter(
            recipient_type=GroupIndividual.RecipientType.SECONDARY
        ).first()

        group_members = {
            str(
                individual.individual.id
            ): f"{individual.individual.first_name} {individual.individual.last_name}"
            for individual in group_individuals
        }

        head_str = (
            f"{head.individual.first_name} {head.individual.last_name}"
            if head
            else None
        )
        head_id = str(head.individual.id) if head else None
        head_json_ext = (
            head.individual.json_ext if head and head.individual.json_ext else {}
        )

        primary_str = (
            f"{primary.individual.first_name} {primary.individual.last_name}"
            if primary
            else None
        )
        primary_id = str(primary.individual.id) if primary else None

        secondary_str = (
            f"{secondary.individual.first_name} {secondary.individual.last_name}"
            if secondary
            else None
        )
        secondary_id = str(secondary.individual.id) if secondary else None

        changes_to_save = {}
        json_ext_minus_keys = {
            k: v
            for k, v in group.json_ext.items()
            if k
            not in [
                "members",
                "head",
                "head_id",
                "primary_recipient",
                "primary_recipient_id",
                "secondary_recipient",
                "secondary_recipient_id",
            ]
        }

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
            p_cutoff = None
            if isinstance(head_json_ext, dict):
                p_score = head_json_ext.get("pmt_score")
                p_class = head_json_ext.get("pmt_class")
                p_cutoff = head_json_ext.get("pmt_cutoff_used")
            if (
                (group.json_ext.get("pmt_score_household") != p_score)
                or (group.json_ext.get("pmt_class_household") != p_class)
                or (group.json_ext.get("pmt_cutoff_used") != p_cutoff)
            ):
                group.json_ext["pmt_score_household"] = p_score
                group.json_ext["pmt_class_household"] = p_class
                if p_cutoff is not None:
                    group.json_ext["pmt_cutoff_used"] = p_cutoff
        except Exception:
            logger.debug("PMT mirror to group json_ext failed", exc_info=True)

        current_members = group.json_ext.get("members", {})
        additional_members = {
            k: v for k, v in group_members.items() if k not in current_members
        }
        remove_members = {
            k: v for k, v in current_members.items() if k not in group_members
        }
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
            group.save(update_fields=["json_ext"], user=self.user)

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
        group_individuals = GroupIndividual.objects.filter(
            group=group, is_deleted=False
        )
        primary_exists = group_individuals.filter(
            recipient_type=GroupIndividual.RecipientType.PRIMARY
        ).exists()
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
        heads_queryset = GroupIndividual.objects.filter(
            group_id=group_id, role=GroupIndividual.Role.HEAD
        )
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


def get_individual_duplication_aggregation(columns, location_id=None):
    """
    Find duplicate individuals based on specified columns.

    Args:
        columns: List of field names to group by (e.g., ['first_name', 'last_name'])
        location_id: Optional location filter (integer ID or string)

    Returns:
        List of groups with count > 1 (actual duplicates)
    """
    from django.contrib.postgres.aggregates import ArrayAgg

    raw_columns = columns
    columns = _normalize_individual_deduplication_columns(columns)

    if not columns:
        return []

    # Separate model fields from json_ext fields
    model_columns, json_columns = _resolve_individual_columns(columns)

    if not model_columns:
        logger.warning(
            "Individual deduplication scan has no valid model columns. raw_columns=%s normalized_columns=%s",
            raw_columns,
            columns,
        )
        return []

    query = Individual.objects.filter(is_deleted=False)

    # Handle location_id filtering - convert string to int if needed
    if location_id:
        try:
            # location_id should be an integer, but handle string input
            if isinstance(location_id, str):
                location_id = int(location_id)
            query = query.filter(location_id=location_id)
        except (ValueError, TypeError):
            logger.warning(f"Invalid location_id provided: {location_id}")
            # Continue without location filter if invalid

    # Group by model columns
    grouped = query.values(*model_columns).annotate(
        count=Count('id'),
        ids=ArrayAgg('id')
    ).filter(count__gt=1)

    # Format results
    results = []
    for group in grouped:
        results.append({
            'count': group['count'],
            'ids': [str(id_) for id_ in group['ids']],
            'column_values': {
                col: _serialize_deduplication_column_value(group.get(col))
                for col in model_columns
            }
        })

    logger.warning(
        "Individual deduplication scan completed. raw_columns=%s normalized_columns=%s model_columns=%s result_count=%s",
        raw_columns,
        columns,
        model_columns,
        len(results),
    )
    return results


def _serialize_deduplication_column_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _normalize_individual_deduplication_columns(columns):
    column_aliases = {
        "firstName": "first_name",
        "individual.firstName": "first_name",
        "individual_firstName": "first_name",
        "Individual First Name": "first_name",
        "First Name": "first_name",
        "firstname": "first_name",
        "lastName": "last_name",
        "individual.lastName": "last_name",
        "individual_lastName": "last_name",
        "Individual Last Name": "last_name",
        "Last Name": "last_name",
        "lastname": "last_name",
        "dateOfBirth": "dob",
        "birthDate": "dob",
        "individual.dob": "dob",
        "Date Of Birth": "dob",
        "Date of Birth": "dob",
        "Birth Date": "dob",
        "location": "location__name",
        "Location": "location__name",
        "village": "location__name",
        "Village": "location__name",
    }

    normalized_columns = []
    for column in columns or []:
        if not column:
            continue
        if isinstance(column, dict):
            column = column.get("id") or column.get("name") or column.get("label")
        column = str(column)
        canonical_column = "".join(ch for ch in column.lower() if ch.isalnum())
        if canonical_column in {"firstname", "individualfirstname"}:
            column = "first_name"
        elif canonical_column in {"lastname", "individuallastname"}:
            column = "last_name"
        elif canonical_column in {"dob", "dateofbirth", "birthdate", "individualdob"}:
            column = "dob"
        elif canonical_column in {"location", "locationname", "village", "villagename"}:
            column = "location__name"
        elif canonical_column in {"locationcode", "villagecode"}:
            column = "location__code"
        else:
            column = column_aliases.get(column, column)
        if column not in normalized_columns:
            normalized_columns.append(column)
    return normalized_columns


def _resolve_individual_columns(columns):
    """
    Separate database model fields from json_ext fields.

    Returns:
        Tuple of (model_columns, json_columns)
    """
    model_columns = []
    json_columns = []

    for col in columns:
        if _is_individual_model_column(col):
            model_columns.append(col)
        else:
            json_columns.append(col)

    return model_columns, json_columns


def _is_individual_model_column(column_name):
    """Check if a column maps to a model field on Individual.

    Supports related lookups such as ``location__name`` / ``location__code``
    by checking the first segment against the model fields.
    """
    try:
        Individual._meta.get_field(str(column_name).split("__", 1)[0])
        return True
    except Exception:
        return False


class CreateDeduplicationIndividualReviewTasksService:
    """Service for creating deduplication review tasks for individuals."""

    TASK_SOURCE = "CreateDeduplicationIndividualReviewTasksService"

    def __init__(self, user, validation_class=None):
        self.user = user
        self.validation_class = validation_class

    @staticmethod
    def _normalize_summary_item(item):
        if isinstance(item, str):
            item = json.loads(item)
        elif hasattr(item, "items"):
            item = dict(item)
        else:
            item = dict(item or {})

        column_values = item.get("column_values", item.get("columnValues", {}))
        if isinstance(column_values, str):
            try:
                column_values = json.loads(column_values)
            except json.JSONDecodeError:
                column_values = {}

        return {
            "count": item.get("count"),
            "ids": [str(id_) for id_ in item.get("ids", [])],
            "column_values": column_values or {},
            "primary_id": item.get("primary_id") or item.get("primaryId"),
        }

    @staticmethod
    def _serialize_individual_for_deduplication(individual):
        json_ext = individual.json_ext or {}
        return {
            "uuid": str(individual.id),
            "individual": {
                "uuid": str(individual.id),
                "first_name": individual.first_name,
                "last_name": individual.last_name,
                "dob": individual.dob.isoformat() if individual.dob else None,
                "location": individual.location.name if individual.location else None,
                "date_created": individual.date_created.isoformat() if individual.date_created else None,
            },
            "json_ext": json_ext,
            "date_created": individual.date_created.isoformat() if individual.date_created else None,
            "is_deleted": individual.is_deleted,
        }

    @staticmethod
    def _headers_for_summary_item(item):
        headers = ["individual", "first_name", "last_name", "dob", "location"]
        for key in item.get("column_values", {}).keys():
            if key not in headers:
                headers.append(key)
        return headers

    def create_individual_duplication_tasks(self, summary):
        """
        Create deduplication review tasks from duplicate summary.

        Args:
            summary: List of duplicate group objects from frontend

        Returns:
            Success/error response
        """
        from tasks_management.services import TaskService
        from tasks_management.apps import TasksManagementConfig

        try:
            task_service = TaskService(self.user)

            normalized_summary = [
                self._normalize_summary_item(item)
                for item in (summary or [])
            ]
            normalized_summary = [item for item in normalized_summary if item.get("ids")]

            if not normalized_summary:
                return output_result_success({"detail": "No duplicates to process"})

            created = []
            for item in normalized_summary:
                individuals = list(
                    Individual.objects.filter(
                        id__in=item["ids"],
                        is_deleted=False,
                    ).select_related("location").order_by("date_created", "id")
                )
                if len(individuals) < 2:
                    continue

                task_data = {
                    'source': self.TASK_SOURCE,
                    'status': Task.Status.RECEIVED,
                    'executor_action_event': TasksManagementConfig.default_executor_event,
                    'business_data_serializer': f'{self.__class__.__module__}.{self.__class__.__name__}.create_individual_duplication_task_serializer',
                    'business_event': IndividualConfig.deduplication_review_event,
                    'data': {
                        'ids': [
                            self._serialize_individual_for_deduplication(individual)
                            for individual in individuals
                        ],
                        'primary_id': item.get("primary_id") or str(individuals[0].id),
                        'column_values': item.get('column_values', {}),
                        'count': len(individuals),
                        'headers': self._headers_for_summary_item(item),
                    }
                }
                result = task_service.create(task_data)
                created.append(result)

            if not created:
                return output_result_success({"detail": "No duplicate tasks to create"})

            errors = []
            for result in created:
                if result and not result.get("success", False):
                    errors.extend(result.get("errors", []))
            if errors:
                return {"success": False, "errors": errors}
            return output_result_success({"detail": f"{len(created)} deduplication task(s) created"})

        except Exception as exc:
            return output_exception(
                model_name='Individual',
                method='create_individual_duplication_tasks',
                exception=exc
            )

    @staticmethod
    def create_individual_duplication_task_serializer(data):
        """Return the deduplication task business data for the Tasks UI.

        ``data`` is already JSON-safe and already shaped the way the frontend
        formatter (``IndividualDeduplicationTaskDisplay``) expects::

            {"ids": [{"individual": {...}, "json_ext": {...}, "uuid": ...}, ...],
             "headers": [...], "column_values": {...}, "count": N, "primary_id": ...}

        Do NOT run it through ``crud_business_data_builder`` - that helper
        assumes the CRUD-update shape ``{"incoming_data": {...}, "current_data": {...}}``
        and raises ``AttributeError`` on the ``ids`` list, which makes
        ``TaskGQLType.resolve_business_data`` return an error string and the task
        renders blank.
        """
        return data

    @classmethod
    def get_class_name(cls):
        return cls.__name__


def _parse_deduplication_resolve_data(resolve_data):
    if isinstance(resolve_data, str):
        try:
            resolve_data = json.loads(resolve_data)
        except json.JSONDecodeError:
            resolve_data = json.loads(resolve_data.replace('\\"', '"'))

    if not isinstance(resolve_data, dict):
        return {}, []

    values = resolve_data.get("values") or {}
    selected_ids = resolve_data.get("beneficiaryIds") or resolve_data.get("individualIds") or []
    return values, [str(id_) for id_ in selected_ids]


def _extract_additional_resolve_data(task, task_payload=None):
    task_payload = task_payload or {}
    json_ext = getattr(task, "json_ext", None) or task_payload.get("json_ext") or {}
    additional = json_ext.get("additional_resolve_data") or task_payload.get("additional_resolve_data")
    if isinstance(additional, dict) and additional:
        first = next(iter(additional.values()))
        return first
    return additional or {}


@transaction.atomic
def merge_duplicate_individuals(task_data, user, resolve_data=None):
    """
    Merge duplicate individuals into the primary record.

    Called when a deduplication task is completed. Can be triggered by:
    - Task management system on task completion
    - Manual invocation after task review

    Args:
        task_data: Dictionary with task business data containing:
                   - 'primary_id': UUID of individual to keep
                   - 'ids': List of all duplicate individual IDs
        user: User performing the merge
    """
    values, selected_ids = _parse_deduplication_resolve_data(resolve_data or {})
    task_ids = task_data.get('ids', [])
    normalized_task_ids = []
    for item in task_ids:
        if isinstance(item, dict):
            normalized_task_ids.append(str(item.get("uuid") or item.get("id")))
        else:
            normalized_task_ids.append(str(item))

    selected_ids = selected_ids or normalized_task_ids
    primary_id = task_data.get('primary_id')
    duplicate_ids = selected_ids

    if not primary_id:
        primary_id = selected_ids[0] if selected_ids else None
    if not primary_id:
        logger.warning("No primary_id specified in deduplication task data")
        return

    try:
        selected_individuals = list(
            Individual.objects.select_for_update()
            .filter(id__in=selected_ids, is_deleted=False)
            .order_by("date_created", "id")
        )
        if len(selected_individuals) < 2:
            logger.info("Deduplication task has fewer than two active selected individuals")
            return

        primary = next((i for i in selected_individuals if str(i.id) == str(primary_id)), selected_individuals[0])
        primary_id = str(primary.id)
        duplicates = Individual.objects.filter(
            id__in=duplicate_ids
        ).exclude(id=primary_id)

        for field, value in values.items():
            if field in {"individual", "location", "date_created", "is_deleted"}:
                continue
            if hasattr(primary, field):
                setattr(primary, field, value)
            else:
                primary.json_ext = primary.json_ext or {}
                primary.json_ext[field] = value
        primary.save(user=user)

        for duplicate in duplicates:
            group_individuals = GroupIndividual.objects.filter(
                individual=duplicate,
                is_deleted=False
            )
            for group_individual in group_individuals:
                group_individual.individual = primary
                group_individual.save(user=user)

            # Transfer Beneficiary relationships if module installed
            try:
                from social_protection.models import Beneficiary
                beneficiaries = Beneficiary.objects.filter(
                    individual=duplicate,
                    is_deleted=False
                )
                for beneficiary in beneficiaries:
                    beneficiary.individual = primary
                    beneficiary.save(user=user)
            except ImportError:
                pass  # Module not installed

            # Use the openIMIS HistoryModel delete path so audit and validity fields stay consistent.
            duplicate.delete(user=user)

        logger.info(
            f"Successfully merged {len(duplicates)} duplicate individuals into {primary_id}"
        )

    except Individual.DoesNotExist:
        logger.error(f"Primary individual {primary_id} not found")
    except Exception as exc:
        logger.exception(
            "Error merging duplicate individuals",
            exc_info=True
        )


def complete_deduplication_task(task, user, task_payload=None):
    task_data = getattr(task, "data", None) or getattr(task, "business_data", None)
    if not task_data:
        task_data = (task_payload or {}).get("data") or (task_payload or {}).get("business_data") or {}
    resolve_data = _extract_additional_resolve_data(task, task_payload)
    merge_duplicate_individuals(task_data, user, resolve_data)


class IndividualImportService:
    import_loaders = {
        # .csv
        "text/csv": lambda f: pd.read_csv(f),
        # .xlsx
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": lambda f: pd.read_excel(
            f
        ),
        # .xls
        "application/vnd.ms-excel": lambda f: pd.read_excel(f),
        # .ods
        "application/vnd.oasis.opendocument.spreadsheet": lambda f: pd.read_excel(f),
    }

    def __init__(self, user):
        super().__init__()
        self.user = user

    @staticmethod
    def _group_individual_role_from_label(role_label):
        if not role_label:
            return None
        normalized_role = (
            str(role_label)
            .strip()
            .upper()
            .replace(" ", "_")
            .replace("-", "_")
        )
        return getattr(GroupIndividual.Role, normalized_role, None)

    @staticmethod
    def _json_ext_lookup(json_ext, *keys):
        """
        Look up a value by key, walking top-level then nested
        json_ext.json_ext and json_ext.json_ext.raw. Returns the first
        non-empty match. Robust to importer payloads that nest the original
        adapter dict (e.g. when role fields aren't promoted to CSV columns).
        """
        if not isinstance(json_ext, dict):
            return None
        candidates = [json_ext]
        nested = json_ext.get("json_ext")
        if isinstance(nested, dict):
            candidates.append(nested)
            raw = nested.get("raw")
            if isinstance(raw, dict):
                candidates.append(raw)
        for source in candidates:
            for key in keys:
                value = source.get(key)
                if value not in (None, ""):
                    return value
        return None

    @classmethod
    def _group_individual_role_from_relationship(cls, relationship_to_head, gender):
        relationship_code = str(relationship_to_head or "").strip()
        normalized_gender = str(gender or "").strip().upper()

        if not relationship_code:
            return None
        if relationship_code == "1":
            return GroupIndividual.Role.HEAD
        if relationship_code in ("2", "12"):
            return GroupIndividual.Role.SPOUSE
        if relationship_code in ("3", "4"):
            if normalized_gender == "M":
                return GroupIndividual.Role.SON
            if normalized_gender == "F":
                return GroupIndividual.Role.DAUGHTER
            return GroupIndividual.Role.OTHER_RELATIVE
        if relationship_code == "5":
            if normalized_gender == "M":
                return GroupIndividual.Role.BROTHER
            if normalized_gender == "F":
                return GroupIndividual.Role.SISTER
            return GroupIndividual.Role.OTHER_RELATIVE
        if relationship_code == "6":
            if normalized_gender == "M":
                return GroupIndividual.Role.GRANDSON
            if normalized_gender == "F":
                return GroupIndividual.Role.GRANDDAUGHTER
            return GroupIndividual.Role.OTHER_RELATIVE
        if relationship_code == "7":
            if normalized_gender == "M":
                return GroupIndividual.Role.FATHER
            if normalized_gender == "F":
                return GroupIndividual.Role.MOTHER
            return GroupIndividual.Role.OTHER_RELATIVE
        if relationship_code == "14":
            return GroupIndividual.Role.NOT_RELATED
        return GroupIndividual.Role.OTHER_RELATIVE

    @classmethod
    def _group_individual_role_from_json_ext(cls, json_ext):
        json_ext = json_ext or {}
        role_from_label = cls._group_individual_role_from_label(
            cls._json_ext_lookup(json_ext, "individual_role")
        )
        if role_from_label:
            return role_from_label

        relationship_to_head = cls._json_ext_lookup(
            json_ext,
            "rel_to_hhh",
            "individual_role_code",
            "relationship_to_head",
        )
        return cls._group_individual_role_from_relationship(
            relationship_to_head,
            cls._json_ext_lookup(json_ext, "gender"),
        )

    @classmethod
    def _group_link_sort_key(cls, individual):
        """
        Keep group-linking deterministic and process the actual household HEAD
        first. This avoids temporary HEAD/PRIMARY auto-promotions on the first
        linked member that later get nulled when the real head is added.
        """
        json_ext = individual.json_ext or {}
        role_code = str(
            cls._json_ext_lookup(
                json_ext,
                "individual_role_code",
                "rel_to_hhh",
                "relationship_to_head",
            )
            or ""
        ).strip()
        hhrep_code = str(cls._json_ext_lookup(json_ext, "hhrep") or "").strip()
        desired_role = cls._group_individual_role_from_json_ext(json_ext)
        desired_primary = bool(hhrep_code and hhrep_code == role_code)

        member_ordinal = cls._json_ext_lookup(
            json_ext,
            "member_ordinal",
            "roster_index",
            "roster__index",
            "hhroster__id",
            "memberlineno",
        )
        try:
            ordinal_rank = int(str(member_ordinal).strip())
        except (TypeError, ValueError):
            ordinal_rank = 10 ** 9

        role_rank = 2
        if desired_role == GroupIndividual.Role.HEAD:
            role_rank = 0
        elif desired_primary:
            role_rank = 1

        return role_rank, ordinal_rank, str(individual.id)

    @register_service_signal("individual.import_individuals")
    def import_individuals(
        self,
        import_file: InMemoryUploadedFile,
        workflow: WorkflowHandler,
        group_aggregation_column: str,
    ):
        upload = self._save_sources(import_file)
        self._create_individual_data_upload_records(
            workflow, upload, group_aggregation_column
        )
        self._trigger_workflow(workflow, upload)
        return {"success": True, "data": {"upload_uuid": upload.uuid}}

    @transaction.atomic
    def _save_sources(self, import_file):
        # Method separated as workflow execution must be independent of the atomic transaction.
        upload = self._create_upload_entry(import_file.name)
        dataframe = self._load_import_file(import_file)
        self._validate_dataframe(dataframe)
        self._save_data_source(dataframe, upload)
        return upload

    @transaction.atomic
    def _create_individual_data_upload_records(
        self, workflow, upload, group_aggregation_column
    ):
        record = IndividualDataUploadRecords(
            data_upload=upload,
            workflow=workflow.name if hasattr(workflow, "name") else str(workflow),
            json_ext={"group_aggregation_column": group_aggregation_column},
        )
        record.save(user=self.user)

    def validate_import_individuals(self, upload_id: uuid, individual_sources):
        dataframe = load_dataframe(individual_sources)
        validated_dataframe, invalid_items = self._validate_possible_individuals(
            dataframe, upload_id
        )
        return {
            "success": True,
            "data": validated_dataframe,
            "summary_invalid_items": invalid_items,
        }

    def synchronize_data_for_reporting(self, upload_id: uuid):
        if "opensearch_reports" in apps.app_configs:
            from individual.documents import IndividualDocument

            individuals = Individual.objects.filter(
                individualdatasource__upload=upload_id
            )
            if not individuals:
                return

            IndividualDocument().update(individuals, "index")

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
        check_location = "location_name" in chunk.columns

        for _, row in chunk.iterrows():
            field_validation = {"row": row.to_dict(), "validations": {}}
            for field, field_properties in properties.items():

                # Validation Calculation
                if "validationCalculation" in field_properties and field in row:
                    field_validation["validations"][field] = (
                        IndividualImportService._handle_validation_calculation(
                            row, field, field_properties
                        )
                    )

                # Uniqueness Check
                if "uniqueness" in field_properties and field in row:
                    field_validation["validations"][f"{field}_uniqueness"] = (
                        IndividualImportService._handle_uniqueness(
                            row, field, unique_validations
                        )
                    )

            if "location_name" in chunk.columns:
                field_validation["validations"]["location_name"] = (
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

        unique_fields = [
            field for field, props in properties.items() if "uniqueness" in props
        ]
        unique_validations = {}
        if unique_fields:
            unique_validations = {
                field: dataframe[field].duplicated(keep=False)
                for field in unique_fields
            }

        check_location = "location_name" in dataframe.columns
        if check_location:
            # Issue a single DB query instead of per row for efficiency
            loc_name_code_district_ids_from_db = self._query_location_district_ids(
                dataframe
            )
            user_allowed_loc_ids = LocationManager().get_allowed_ids(self.user)
            duplicate_village_name_code_tuples = (
                self._query_duplicate_village_name_code()
            )
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
        unique_tuples = df[["location_name", "location_code"]].drop_duplicates()
        query = Q()
        for _, row in unique_tuples.iterrows():
            query |= Q(name=row["location_name"], code=row["location_code"])
        locations = Location.objects.filter(type="V", *filter_validity()).filter(query)
        return {(loc.name, loc.code): loc.parent.parent.id for loc in locations}

    @staticmethod
    def _query_duplicate_village_name_code():
        return (
            Location.objects.filter(type="V", *filter_validity())
            .values("name", "code")
            .annotate(name_count=Count("id"))
            .filter(name_count__gt=1)
            .values_list("name", "code")
        )

    @staticmethod
    def _validate_location(
        location_name,
        location_code,
        loc_name_code_district_ids_from_db,
        user_allowed_loc_ids,
        duplicate_village_name_code_tuples,
    ):
        """
        Validate location by ALWAYS normalizing the code to 9 digits.
        This ensures compatibility with openIMIS DB codes and fixes UI display issues.
        """
        result = {"field_name": "location_name"}

        # --- NORMALIZE THE CODE ---
        code = "" if pd.isna(location_code) else str(location_code).strip()
        code = code.replace(".0", "")  # remove Excel float
        code = "".join(ch for ch in code if ch.isdigit())  # digits only

        if code:
            code = code.zfill(9)  # ALWAYS 9 digits for TASAF

        # ---------------- VALIDATION -----------------
        if (pd.isna(location_name) or str(location_name).strip() == "") and code == "":
            result["success"] = True

        elif (
            loc_name_code_district_ids_from_db is None and user_allowed_loc_ids is None
        ):
            result["success"] = True

        elif (location_name, code) not in loc_name_code_district_ids_from_db:
            result["success"] = False
            result["note"] = (
                f"Location with name '{location_name}' and code '{code}' is not valid. "
                "Please check the spelling against the list of locations in the system."
            )

        elif (location_name, code) in duplicate_village_name_code_tuples:
            result["success"] = False
            result["note"] = (
                f"Location with name '{location_name}' and code '{code}' is ambiguous, "
                "because more than one matching location exists."
            )

        elif (
            loc_name_code_district_ids_from_db[(location_name, code)]
            not in user_allowed_loc_ids
        ):
            result["success"] = False
            result["note"] = (
                f"Location with name '{location_name}' and code '{code}' is outside the current user's location permissions."
            )

        else:
            result["success"] = True

        return result

    @staticmethod
    def _normalize_code_series(series: pd.Series, width: int) -> pd.Series:
        """
        Normalize a code column coming from CSV/Excel:
        - convert to string, strip
        - remove trailing '.0'
        - keep only digits
        - pad left to fixed width
        """
        s = series.astype(str).str.strip()
        s = s.str.replace(r"\.0$", "", regex=True)
        s = s.str.replace(r"[^0-9]", "", regex=True)
        mask = s.str.len() > 0
        s.loc[mask] = s.loc[mask].str.zfill(width)
        return s

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
        validation_calculation = field_properties.get("validationCalculation", {}).get(
            "name"
        )
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
        upload = IndividualDataSourceUpload(
            source_name=filename, source_type="individual import"
        )
        upload.save(username=self.user.login_name)
        return upload

    def _validate_dataframe(self, dataframe: pd.DataFrame):
        if dataframe is None:
            raise ValueError("Unknown error while loading import file")
        if dataframe.empty:
            raise ValueError("Import file is empty")

    def _load_import_file(self, import_file) -> pd.DataFrame:
        """
        Load CSV/Excel and normalize all location-related code fields.
        This restores original openIMIS behaviour and ensures UI can match locations.
        """
        if import_file.content_type not in self.import_loaders:
            raise ValueError(f"Unsupported content type: {import_file.content_type}")

        # Load using registered loader
        df = self.import_loaders[import_file.content_type](import_file)

        # --- Normalize codes for proper UI + validation behaviour ---
        # These columns may appear depending on the import template
        if "location_code" in df.columns:
            df["location_code"] = self._normalize_code_series(df["location_code"], 9)

        if "ward_code" in df.columns:
            df["ward_code"] = self._normalize_code_series(df["ward_code"], 6)

        if "district_code" in df.columns:
            df["district_code"] = self._normalize_code_series(df["district_code"], 4)

        if "region_code" in df.columns:
            df["region_code"] = self._normalize_code_series(df["region_code"], 2)

        return df

    # def _load_import_file(self, import_file) -> pd.DataFrame:
    #     if import_file.content_type not in self.import_loaders:
    #         raise ValueError("Unsupported content type: {}".format(import_file.content_type))
    #     return self.import_loaders[import_file.content_type](import_file)

    def _save_data_source(
        self, dataframe: pd.DataFrame, upload: IndividualDataSourceUpload
    ):
        """
        Save each uploaded row as IndividualDataSource.json_ext.

        Key fix:
        - If the incoming dataframe has a 'json_ext' column, it often contains a JSON STRING
        (because ETL writes json_ext as serialized JSON in the CSV).
        - If we store it as-is, we end up with:
            {"json_ext": "{...string...}", ...}
        which later propagates to Individual.Json_ext and causes json_ext->'json_ext' to be a string.
        - Here we parse that string into a dict and merge it into the row payload.
        """
        data_source_objects = []

        for _, row in dataframe.iterrows():
            # Convert row to a plain python dict (safe for JSON dumps/loads)
            row_dict = json.loads(row.to_json())

            # ---- FIX: parse json_ext string -> dict and inline it properly ----
            jx = row_dict.get("json_ext")

            if isinstance(jx, str):
                # Sometimes pandas stores NaN-like values as "nan" string
                jx_str = jx.strip()
                if jx_str and jx_str.lower() not in ("nan", "none", "null"):
                    try:
                        parsed = json.loads(jx_str)
                        if isinstance(parsed, dict):
                            row_dict["json_ext"] = parsed
                        else:
                            # If it's valid JSON but not an object, keep as empty object
                            row_dict["json_ext"] = {}
                    except Exception:
                        # If parsing fails, keep an empty object rather than a broken string
                        row_dict["json_ext"] = {}
                else:
                    row_dict["json_ext"] = {}

            elif jx is None:
                row_dict["json_ext"] = {}

            # If already dict, keep it as-is
            if not isinstance(row_dict.get("json_ext"), dict):
                row_dict["json_ext"] = {}

            # ---- OPTIONAL: ensure 'raw' is preserved if present (no overwrite) ----
            # (Do nothing special here; we store whatever is inside row_dict["json_ext"].
            # Your ETL already puts questionnaire variables in json_ext["raw"].)

            ds = IndividualDataSource(
                upload=upload,
                json_ext=row_dict,
                validations={},
                user_created=self.user,
                user_updated=self.user,
                uuid=uuid.uuid4(),
            )
            data_source_objects.append(ds)

        IndividualDataSource.objects.bulk_create(data_source_objects)

    def _trigger_workflow(
        self, workflow: WorkflowHandler, upload: IndividualDataSourceUpload
    ):
        """
        Trigger the configured workflow for this upload, with proper status transitions
        and error capture (matches original behavior). Also supports dotted-path/callables.
        """
        # if no workflow
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

            result = runner.run(
                {
                    "user_uuid": user_uuid,
                    "upload_uuid": str(upload.uuid),
                }
            )

            # Structured failure from handler -> mark FAIL and record error
            if result and isinstance(result, dict) and result.get("success") is False:
                raise ValueError(
                    result.get(
                        "message", "Unexpected error during the workflow execution"
                    )
                )

        except ValueError as e:
            upload.status = IndividualDataSourceUpload.Status.FAIL
            upload.error = {"workflow": str(e)}
            upload.save(username=self.user.login_name)
            return upload
        except Exception as e:
            upload.status = IndividualDataSourceUpload.Status.FAIL
            upload.error = {"workflow": str(e)}
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
            Individual,
            Group,
            GroupIndividual,
            IndividualDataUploadRecords,
            IndividualDataSourceUpload,
        )
        from individual.services import (
            GroupIndividualService,
            GroupAndGroupIndividualAlignmentService,
        )

        upload = IndividualDataSourceUpload.objects.filter(
            uuid=upload_uuid, is_deleted=False
        ).first()
        if not upload:
            return {"success": False, "message": f"Upload {upload_uuid} not found"}

        # Determine grouping column (defaults to 'group_code')
        group_col = "group_code"
        rec = (
            IndividualDataUploadRecords.objects.filter(
                data_upload=upload, is_deleted=False
            )
            .order_by("id")
            .first()
        )
        if rec and isinstance(rec.json_ext, dict):
            c = (rec.json_ext or {}).get("group_aggregation_column")
            if isinstance(c, str) and c.strip():
                group_col = c.strip()

        inds = list(Individual.objects.filter(
            individualdatasource__upload=upload, is_deleted=False
        ).distinct())
        inds.sort(key=self._group_link_sort_key)

        if not inds:
            return {
                "success": True,
                "group_column": group_col,
                "created_groups": 0,
                "created_links": 0,
                "updated_links": 0,
                "groups_touched": 0,
            }

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

                # Role / recipient inference from individual's json_ext.
                # Keep this aligned with api_etl's relationship-to-head mapping.
                jx = ind.json_ext or {}
                role_code = str(
                    self._json_ext_lookup(
                        jx,
                        "individual_role_code",
                        "rel_to_hhh",
                        "relationship_to_head",
                    )
                    or ""
                ).strip()
                hhrep_code = str(self._json_ext_lookup(jx, "hhrep") or "").strip()

                desired_role = self._group_individual_role_from_json_ext(jx)
                desired_recipient = (
                    GroupIndividual.RecipientType.PRIMARY
                    if (hhrep_code and hhrep_code == role_code)
                    else None
                )

                # Align locations BEFORE linking (mirrors your earlier logic)
                has_head = GroupIndividual.objects.filter(
                    group=grp, role=GroupIndividual.Role.HEAD, is_deleted=False
                ).exists()
                role_for_alignment = (
                    desired_role
                    if (desired_role == GroupIndividual.Role.HEAD or not has_head)
                    else None
                )
                try:
                    aligner.ensure_location_consistent(grp, ind, role_for_alignment)
                except Exception:
                    # non-fatal alignment error
                    pass

                gi = GroupIndividual.objects.filter(
                    group=grp, individual=ind, is_deleted=False
                ).first()
                if not gi:
                    GroupIndividualService(self.user).create(
                        {
                            "group_id": str(grp.id),
                            "individual_id": str(ind.id),
                            "role": desired_role,
                            "recipient_type": desired_recipient,
                        }
                    )
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
                        pmt_cutoff_used = jx.get("pmt_cutoff_used")
                        if pmt_score is not None or pmt_class is not None or pmt_cutoff_used is not None:
                            upd = False
                            if grp.json_ext is None:
                                grp.json_ext = {}
                            if (
                                pmt_score is not None
                                and grp.json_ext.get("pmt_score_household") != pmt_score
                            ):
                                grp.json_ext["pmt_score_household"] = pmt_score
                                upd = True
                            if (
                                pmt_class is not None
                                and grp.json_ext.get("pmt_class_household") != pmt_class
                            ):
                                grp.json_ext["pmt_class_household"] = pmt_class
                                upd = True
                            if (
                                pmt_cutoff_used is not None
                                and grp.json_ext.get("pmt_cutoff_used") != pmt_cutoff_used
                            ):
                                grp.json_ext["pmt_cutoff_used"] = pmt_cutoff_used
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

        up = IndividualDataSourceUpload.objects.filter(
            id=upload_id, is_deleted=False
        ).first()
        if not up:
            return {"success": False, "message": f"Upload id {upload_id} not found"}
        return self.link_groups_for_upload_uuid(str(up.uuid))

    # Backward-compatible alias if any caller expects this name
    def finalize_upload_group_links(self, upload_uuid: str) -> dict:
        return self.link_groups_for_upload_uuid(upload_uuid)

    def save_validation_error_in_data_source_bulk(self, validated_dataframe):
        data_sources_to_update = []

        for field_validation in validated_dataframe:
            row = field_validation["row"]
            error_fields = []

            for key, value in field_validation["validations"].items():
                if not value.get("success", False):
                    error_fields.append(
                        {
                            "field_name": value.get("field_name"),
                            "note": value.get("note"),
                        }
                    )

            data_sources_to_update.append(
                IndividualDataSource(
                    id=row["id"], validations={"validation_errors": error_fields}
                )
            )

        if data_sources_to_update:
            IndividualDataSource.objects.bulk_update(
                data_sources_to_update, ["validations"]
            )

    def create_task_with_importing_valid_items(self, upload_id: uuid):
        if IndividualConfig.enable_maker_checker_for_individual_upload:
            IndividualTaskCreatorService(
                self.user
            ).create_task_with_importing_valid_items(upload_id)
        else:
            record = IndividualDataUploadRecords.objects.get(
                data_upload_id=upload_id, is_deleted=False
            )
            from individual.signals.on_validation_import_valid_items import (
                IndividualItemsImportTaskCompletionEvent,
            )

            IndividualItemsImportTaskCompletionEvent(
                IndividualConfig.validation_import_valid_items_workflow,
                record,
                record.data_upload.id,
                self.user,
            ).run_workflow()

    def create_task_with_update_valid_items(self, upload_id: uuid):
        # Resolve automatically if maker-checker not enabled
        if IndividualConfig.enable_maker_checker_for_individual_update:
            IndividualTaskCreatorService(self.user).create_task_with_update_valid_items(
                upload_id
            )
        else:
            record = IndividualDataUploadRecords.objects.get(
                data_upload_id=upload_id, is_deleted=False
            )
            from individual.signals.on_validation_import_valid_items import (
                IndividualItemsUploadTaskCompletionEvent,
            )

            IndividualItemsUploadTaskCompletionEvent(
                IndividualConfig.validation_upload_valid_items_workflow,
                record,
                record.data_upload.id,
                self.user,
            ).run_workflow()


class IndividualTaskCreatorService:

    def __init__(self, user):
        self.user = user

    def create_task_with_importing_valid_items(self, upload_id: uuid):
        self._create_task(upload_id, IndividualConfig.validation_import_valid_items)

    def create_task_with_update_valid_items(self, upload_id: uuid):
        self._create_task(upload_id, IndividualConfig.validation_upload_valid_items)

    @register_service_signal("individual.update_task")
    @transaction.atomic()
    def _create_task(self, upload_id, business_event):
        from tasks_management.services import TaskService
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.models import Task

        upload_record = IndividualDataUploadRecords.objects.get(
            data_upload_id=upload_id, is_deleted=False
        )
        json_ext = {
            "source_name": upload_record.data_upload.source_name,
            "workflow": upload_record.workflow,
            "percentage_of_invalid_items": self.__calculate_percentage_of_invalid_items(
                upload_id
            ),
            "data_upload_id": str(upload_id),
            "group_aggregation_column": (
                upload_record.json_ext.get("group_aggregation_column")
                if isinstance(upload_record.json_ext, dict)
                else None
            ),
        }
        TaskService(self.user).create(
            {
                "source": "import_valid_items",
                "entity": upload_record,
                "status": Task.Status.RECEIVED,
                "executor_action_event": TasksManagementConfig.default_executor_event,
                "business_event": business_event,
                "json_ext": json_ext,
            }
        )

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
