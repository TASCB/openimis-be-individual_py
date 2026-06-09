import graphene
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.db.models import Subquery, Q
from django.utils.translation import gettext as _
import logging

from core import filter_validity
from core.gql.gql_mutations.base_mutation import BaseHistoryModelDeleteMutationMixin, BaseMutation, \
    BaseHistoryModelUpdateMutationMixin, BaseHistoryModelCreateMutationMixin
from core.schema import OpenIMISMutation
from individual.apps import IndividualConfig
from individual.models import Individual, Group, GroupIndividual, PmtGlobalFormula
from individual.services import IndividualService, GroupService, GroupIndividualService, \
    CreateGroupAndMoveIndividualService, CreateDeduplicationIndividualReviewTasksService
from location.models import Location, LocationManager

logger = logging.getLogger(__name__)


class CreateIndividualInputType(OpenIMISMutation.Input):
    first_name = graphene.String(required=True, max_length=255)
    last_name = graphene.String(required=True, max_length=255)
    dob = graphene.Date(required=True)
    json_ext = graphene.types.json.JSONString(required=False)
    location_id = graphene.Int(required=False)


class UpdateIndividualInputType(CreateIndividualInputType):
    id = graphene.UUID(required=True)


RoleEnum = graphene.Enum.from_enum(GroupIndividual.Role)
RecipientTypeEnum = graphene.Enum.from_enum(GroupIndividual.RecipientType)


class CreateGroupIndividualInputType(OpenIMISMutation.Input):
    group_id = graphene.UUID(required=False)
    individual_id = graphene.UUID(required=True)
    role = graphene.Field(RoleEnum, required=False)
    recipient_type = graphene.Field(RecipientTypeEnum, required=False)

    def resolve_role(self, info):
        return self.role

    def resolve_recipient_type(self, info):
        return self.recipient_type


class CreateGroupIndividualInputTypeInputObjectType(graphene.InputObjectType):
    group_id = graphene.UUID(required=False)
    individual_id = graphene.UUID(required=True)
    role = graphene.Field(RoleEnum, required=False)
    recipient_type = graphene.Field(RecipientTypeEnum, required=False)

    def resolve_role(self, info):
        return self.role

    def resolve_recipient_type(self, info):
        return self.recipient_type


class CreateGroupInputType(OpenIMISMutation.Input):
    code = graphene.String(required=True)
    individuals_data = graphene.List(CreateGroupIndividualInputTypeInputObjectType, required=False)
    location_id = graphene.Int(required=False)


class UpdateGroupInputType(OpenIMISMutation.Input):
    id = graphene.UUID(required=True)
    code = graphene.String(required=False)
    individuals_data = graphene.List(CreateGroupIndividualInputTypeInputObjectType, required=False)
    location_id = graphene.Int(required=False)


class UpdateGroupIndividualInputType(CreateGroupIndividualInputType):
    id = graphene.UUID(required=True)


class ConfirmIndividualEnrollmentInputType(OpenIMISMutation.Input):
    custom_filters = graphene.List(required=False, of_type=graphene.String)
    benefit_plan_id = graphene.String(required=True, max_lenght=255)
    status = graphene.String(required=True, max_lenght=255)


class CreateIndividualMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreateIndividualMutation"
    _mutation_module = "individual"
    _model = Individual

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_individual_create_perms):
            raise PermissionDenied(_("unauthorized"))
        if (
            'location_id' in data and
            not LocationManager().is_allowed(
                user,
                [data['location_id']]
            )
        ):
            raise PermissionDenied(_("unauthorized.location"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = IndividualService(user)
        result = service.create(data)
        return result if not result['success'] else None

    class Input(CreateIndividualInputType):
        pass


class UpdateIndividualMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "UpdateIndividualMutation"
    _mutation_module = "individual"
    _model = Individual

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_individual_update_perms):
            raise PermissionDenied(_("unauthorized"))

        location_from = Individual.objects.get(id=data['id']).location_id

        location_to_check = [data['location_id']] if 'location_id' in data else []
        if location_from:
            location_to_check.append(location_from)
        if (
            len(location_to_check)>0 and
            not LocationManager().is_allowed(
                user,
                location_to_check
            )
        ):
            raise PermissionDenied(_("unauthorized.location"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = IndividualService(user)
        if IndividualConfig.check_individual_update:
            result = service.create_update_task(data)
        else:
            result = service.update(data)
        return result if not result['success'] else None

    class Input(UpdateIndividualInputType):
        pass


class DeleteIndividualMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "DeleteIndividualMutation"
    _mutation_module = "individual"
    _model = Individual

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_individual_delete_perms):
            raise PermissionDenied(_("unauthorized"))

        locations_id = list(
            Location.objects.filter(
                individuals__id__in=data['ids'],
                *filter_validity()
            ).values_list('id', flat=True)
        )
        if len(locations_id)>0 and not LocationManager().is_allowed(
                user,
                locations_id
        ):
            raise PermissionDenied(_("unauthorized.location"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = IndividualService(user)

        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for identifier in ids:
                    obj_data = {'id': identifier}
                    if IndividualConfig.check_individual_delete:
                        service.create_delete_task(obj_data)
                    else:
                        service.delete(obj_data)

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class UndoDeleteIndividualMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "UndoDeleteIndividualMutation"
    _mutation_module = "individual"
    _model = Individual

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_individual_undo_delete_perms):
            raise PermissionDenied(_("unauthorized"))

        locations_id = list(
            Location.objects.filter(
                individuals__id__in=data['ids'],
                *filter_validity()
            ).values_list('id', flat=True)
        )
        if len(locations_id)>0 and not LocationManager().is_allowed(
                user,
                locations_id
        ):
            raise PermissionDenied(_("unauthorized.location"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = IndividualService(user)

        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for identifier in ids:
                    service.undo_delete({'id': identifier})

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class CreateGroupMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreateGroupMutation"
    _mutation_module = "individual"
    _model = Group

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_group_create_perms):
            raise PermissionDenied(_("unauthorized"))
        if (
            'location_id' in data and
            not LocationManager().is_allowed(
                user,
                [data['location_id']]
            )
        ):
            raise PermissionDenied(_("unauthorized.location"))
    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = GroupService(user)
        result = service.create(data)
        return result if not result['success'] else None

    class Input(CreateGroupInputType):
        pass


class UpdateGroupMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "UpdateGroupMutation"
    _mutation_module = "individual"
    _model = Group

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_group_update_perms):
            raise PermissionDenied(_("unauthorized"))
        location_from = Group.objects.get(id=data['id']).location_id
        location_to_check = [data['location_id']] if 'location_id' in data else []
        if location_from:
            location_to_check.append(location_from)
        if (
            len(location_to_check)>0 and not LocationManager().is_allowed(
                user,
                location_to_check
            )
        ):
            raise PermissionDenied(_("unauthorized.location"))
    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = GroupService(user)
        result = service.update(data)
        return result if not result['success'] else None

    class Input(UpdateGroupInputType):
        pass


class DeleteGroupMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "DeleteGroupMutation"
    _mutation_module = "social_protection"
    _model = Group

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_group_delete_perms):
            raise PermissionDenied(_("unauthorized"))

        locations_id = list(
            Location.objects.filter(
                groups__id__in=data['ids'],
                *filter_validity()
            ).values_list('id', flat=True)
        )
        if len(locations_id)>0 and not LocationManager().is_allowed(
                user,
                locations_id
        ):
            raise PermissionDenied(_("unauthorized.location"))
    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = GroupService(user)

        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for identifier in ids:
                    obj_data = {'id': identifier}
                    if IndividualConfig.check_group_delete:
                        service.create_delete_task(obj_data)
                    else:
                        service.delete(obj_data)

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class CreateGroupIndividualMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreateGroupIndividualMutation"
    _mutation_module = "individual"
    _model = GroupIndividual

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_group_create_perms):
            raise PermissionDenied(_("unauthorized"))
        group_location_id = Group.objects.get(id=data['group_id']).location_id
        individual_location_id = Individual.objects.get(id=data['individual_id']).location_id
        location_to_check = []
        if group_location_id:
            location_to_check.append(group_location_id)
        if individual_location_id:
            location_to_check.append(individual_location_id)
        if len(location_to_check)>0 and not LocationManager().is_allowed(
                user,
                location_to_check
        ):
            raise PermissionDenied(_("unauthorized.location"))

        if group_location_id and individual_location_id and group_location_id != individual_location_id:
            raise ValidationError(_("mutation.individual_group_location_mismatch"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = GroupIndividualService(user)
        result = service.create(data)
        return result if not result['success'] else None

    class Input(CreateGroupIndividualInputType):
        pass


class UpdateGroupIndividualMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "UpdateGroupIndividualMutation"
    _mutation_module = "individual"
    _model = GroupIndividual

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_group_update_perms):
            raise PermissionDenied(_("unauthorized"))

        group_location_id = Group.objects.get(id=data['group_id']).location_id
        individual_location_id = Individual.objects.get(id=data['individual_id']).location_id
        location_to_check = []
        if individual_location_id:
            location_to_check.append(individual_location_id)
        if group_location_id:
            location_to_check.append(group_location_id)
        if len(location_to_check)>0 and not LocationManager().is_allowed(
                user,
                location_to_check
        ):
            raise PermissionDenied(_("unauthorized.location"))

        if group_location_id and individual_location_id and group_location_id != individual_location_id:
            raise ValidationError(_("mutation.individual_group_location_mismatch"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = GroupIndividualService(user)
        if IndividualConfig.check_group_individual_update:
            result = service.create_update_task(data)
        else:
            result = service.update(data)
        return result if not result['success'] else None

    class Input(UpdateGroupIndividualInputType):
        pass


class DeleteGroupIndividualMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "DeleteGroupIndividualMutation"
    _mutation_module = "individual"
    _model = GroupIndividual

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_group_delete_perms):
            raise PermissionDenied(_("unauthorized"))
        locations_qs = list(
            Location.objects.filter(
                Q(groups__groupindividuals__id__in=data['ids'])|
                Q(individuals__groupindividuals__id__in=data['ids'])
            ).filter(*filter_validity()).values_list('id', flat=True)
        )
        # must first check if locations_qs exists in case none of the groups or individuals has location
        if len(locations_qs)>0 and not LocationManager().is_allowed(
                user,
                locations_qs
        ):
            raise PermissionDenied(_("unauthorized.location"))
    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = GroupIndividualService(user)

        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for identifier in ids:
                    service.delete({'id': identifier})

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class CreateGroupIndividualsMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreateGroupIndividualsMutation"
    _mutation_module = "individual"
    _model = Group

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_group_create_perms):
            raise PermissionDenied(_("unauthorized"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = GroupService(user)
        result = service.create_group_individuals(data)
        return result if not result['success'] else None

    class Input(CreateGroupInputType):
        individual_ids = graphene.List(graphene.UUID, required=True)


class CreateGroupAndMoveIndividualMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreateGroupAndMoveIndividualMutation"
    _mutation_module = "individual"
    _model = Group

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)

        required_perms = IndividualConfig.gql_group_create_perms + IndividualConfig.gql_group_update_perms
        if not user.has_perms(required_perms):
            raise PermissionDenied(_("unauthorized"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = CreateGroupAndMoveIndividualService(user)
        if IndividualConfig.check_group_individual_update or IndividualConfig.check_group_create:
            result = service.create_create_task(data)
        else:
            result = service.create(data)
        return result if not result['success'] else None

    class Input(CreateGroupInputType):
        group_individual_id = graphene.UUID(required=True)


def _is_pct_benefit_plan(benefit_plan_id):
    """
    True when ``benefit_plan_id`` resolves to the configured PCT benefit plan.

    PCT enrollment is driven automatically from PMT eligibility
    (``PctAutoEnrollmentService``); the manual selection-task mutations must not
    be used as a second enrollment surface for that plan. Returns False — i.e.
    allow the standard openIMIS manual enrollment — for every other plan, and
    whenever social_protection is unavailable or the PCT plan is not
    configured/found.
    """
    pct_code = (IndividualConfig.pct_benefit_plan_code or "").strip()
    if not pct_code or not benefit_plan_id:
        return False
    try:
        from social_protection.models import BenefitPlan
    except Exception:
        return False
    return BenefitPlan.objects.filter(
        id=benefit_plan_id,
        code=pct_code,
        is_deleted=False,
    ).exists()


class ConfirmIndividualEnrollmentMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "ConfirmIndividualEnrollmentMutation"
    _mutation_module = "individual"
    _model = Individual

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_group_create_perms):
            raise PermissionDenied(_("unauthorized"))
        if _is_pct_benefit_plan(data.get('benefit_plan_id')):
            raise ValidationError(_("individual.enrollment.pct_manual_blocked"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')
        custom_filters = data.pop('custom_filters', None)
        benefit_plan_id = data.pop('benefit_plan_id', None)
        status = data.pop('status', "ACTIVE")
        service = IndividualService(user)
        service.select_individuals_to_benefit_plan(
            custom_filters,
            benefit_plan_id,
            status,
            user,
        )
        return None

    class Input(ConfirmIndividualEnrollmentInputType):
        pass


class ConfirmGroupEnrollmentMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "ConfirmGroupEnrollmentMutation"
    _mutation_module = "individual"
    _model = Group

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(
                IndividualConfig.gql_group_create_perms):
            raise PermissionDenied(_("unauthorized"))
        if _is_pct_benefit_plan(data.get('benefit_plan_id')):
            raise ValidationError(_("individual.enrollment.pct_manual_blocked"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')
        custom_filters = data.pop('custom_filters', None)
        benefit_plan_id = data.pop('benefit_plan_id', None)
        status = data.pop('status', "ACTIVE")
        service = GroupService(user)
        service.select_groups_to_benefit_plan(
            custom_filters,
            benefit_plan_id,
            status,
            user,
        )
        return None

    class Input(ConfirmIndividualEnrollmentInputType):
        pass


class RerunPmtInputType(OpenIMISMutation.Input):
    district_code = graphene.String(required=True)
    region_code = graphene.String(required=False)
    pmt_cutoff = graphene.Float(required=True)


class RerunPmtMutation(OpenIMISMutation):
    Input = RerunPmtInputType 

    _mutation_class = "RerunPmtMutation"
    _mutation_module = "individual"

    ok = graphene.Boolean()
    errors = graphene.List(graphene.String)
    updated_individuals = graphene.Int()
    updated_groups = graphene.Int()
    mutation_id = graphene.UUID()
    district_code = graphene.String()

    @classmethod
    def mutate_and_get_payload(cls, root, info, **input_data):
        import logging
        import uuid
        from django.contrib.auth.models import AnonymousUser
        from django.core.exceptions import PermissionDenied

        logger = logging.getLogger(__name__)

        district_code = input_data.get("district_code")
        mutation_id = None

        try:
            user = info.context.user if info and info.context else None

            if type(user) is AnonymousUser or not user or not user.id:
                raise PermissionDenied(_("mutation.authentication_required"))

            if not user.has_perms(IndividualConfig.gql_pmt_rerun_perms):
                raise PermissionDenied(_("unauthorized"))

            region_code = input_data.get("region_code")
            pmt_cutoff = input_data.get("pmt_cutoff", 11.01)

            # Generate a mutation_id for progress tracking
            mutation_id = str(uuid.uuid4())

            # Dispatch async Celery task — returns immediately, no timeout
            from individual.tasks import rerun_pmt_task
            rerun_pmt_task.delay(
                district_code=district_code,
                region_code=region_code,
                pmt_cutoff=float(pmt_cutoff),
                user_id=str(user.id),
                mutation_id=mutation_id,
            )

            logger.info(
                f"RerunPmtMutation: dispatched async task "
                f"district={district_code}, mutation_id={mutation_id}"
            )

            # Return immediately — frontend polls PmtRunProgress for completion
            return cls(
                ok=True,
                errors=[],
                updated_individuals=0,
                updated_groups=0,
                mutation_id=mutation_id,
                district_code=district_code,
            )

        except PermissionDenied as e:
            logger.warning(f"RerunPmtMutation: Permission denied: {str(e)}")
            return cls(
                ok=False,
                errors=[str(e)],
                updated_individuals=0,
                updated_groups=0,
                mutation_id=mutation_id,
                district_code=district_code,
            )
        except Exception as e:
            logger.error(
                f"RerunPmtMutation: Unexpected error: {str(e)}", exc_info=True
            )
            return cls(
                ok=False,
                errors=[f"Mutation failed: {str(e)}"],
                updated_individuals=0,
                updated_groups=0,
                mutation_id=mutation_id,
                district_code=district_code,
            )


class AdjustPmtCutoffInputType(OpenIMISMutation.Input):
    district_code = graphene.String(required=True)
    region_code = graphene.String(required=False)
    pmt_cutoff = graphene.Float(required=True)


class AdjustPmtCutoffMutation(OpenIMISMutation):
    Input = AdjustPmtCutoffInputType

    _mutation_class = "AdjustPmtCutoffMutation"
    _mutation_module = "individual"

    ok = graphene.Boolean()
    errors = graphene.List(graphene.String)
    updated_individuals = graphene.Int()
    updated_groups = graphene.Int()
    mutation_id = graphene.UUID()
    district_code = graphene.String()

    @classmethod
    def mutate_and_get_payload(cls, root, info, **input_data):
        import uuid
        from django.contrib.auth.models import AnonymousUser
        from django.core.exceptions import PermissionDenied

        district_code = input_data.get("district_code")
        mutation_id = None

        try:
            user = info.context.user if info and info.context else None

            if type(user) is AnonymousUser or not user or not user.id:
                raise PermissionDenied(_("mutation.authentication_required"))

            if not user.has_perms(IndividualConfig.gql_pmt_rerun_perms):
                raise PermissionDenied(_("unauthorized"))

            region_code = input_data.get("region_code")
            pmt_cutoff = input_data.get("pmt_cutoff", 11.01)
            mutation_id = str(uuid.uuid4())

            from individual.tasks import adjust_pmt_cutoff_task
            adjust_pmt_cutoff_task.delay(
                district_code=district_code,
                region_code=region_code,
                pmt_cutoff=float(pmt_cutoff),
                user_id=str(user.id),
                mutation_id=mutation_id,
            )

            logger.info(
                f"AdjustPmtCutoffMutation: dispatched async task "
                f"district={district_code}, mutation_id={mutation_id}"
            )

            return cls(
                ok=True,
                errors=[],
                updated_individuals=0,
                updated_groups=0,
                mutation_id=mutation_id,
                district_code=district_code,
            )

        except PermissionDenied as e:
            logger.warning(f"AdjustPmtCutoffMutation: Permission denied: {str(e)}")
            return cls(
                ok=False,
                errors=[str(e)],
                updated_individuals=0,
                updated_groups=0,
                mutation_id=mutation_id,
                district_code=district_code,
            )
        except Exception as e:
            logger.error(
                f"AdjustPmtCutoffMutation: Unexpected error: {str(e)}", exc_info=True
            )
            return cls(
                ok=False,
                errors=[f"Mutation failed: {str(e)}"],
                updated_individuals=0,
                updated_groups=0,
                mutation_id=mutation_id,
                district_code=district_code,
            )


# ========================
# PMT Mutations
# ========================

class CreatePmtConfigInputType(OpenIMISMutation.Input):
    location_id = graphene.Int(required=True)
    pmt_cutoff = graphene.Float(required=True)
    is_active = graphene.Boolean(required=False, default_value=True)
    json_ext = graphene.types.json.JSONString(required=False)


class UpdatePmtConfigInputType(OpenIMISMutation.Input):
    id = graphene.UUID(required=True)
    location_id = graphene.Int(required=False)
    pmt_cutoff = graphene.Float(required=False)
    is_active = graphene.Boolean(required=False)
    json_ext = graphene.types.json.JSONString(required=False)


class CreatePmtConfigMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    """
    Create a new PMT Configuration for a location.
    """
    _mutation_class = "CreatePmtConfigMutation"
    _mutation_module = "individual"
    _model = None  # Will be set in _mutate

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(IndividualConfig.gql_pmt_rerun_perms):
            raise PermissionDenied(_("unauthorized"))

        # Verify location exists and user has access
        if 'location_id' in data:
            location_id = data['location_id']
            if not LocationManager().is_allowed(user, [location_id]):
                raise PermissionDenied(_("unauthorized.location"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        from individual.models import PmtConfig
        from individual.services import PmtConfigService

        service = PmtConfigService(user)
        result = service.create(data)
        return result if not result.get('success') else None

    class Input(CreatePmtConfigInputType):
        pass


class UpdatePmtConfigMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    """
    Update an existing PMT Configuration.
    """
    _mutation_class = "UpdatePmtConfigMutation"
    _mutation_module = "individual"
    _model = None  # Will be set in _mutate

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(IndividualConfig.gql_pmt_rerun_perms):
            raise PermissionDenied(_("unauthorized"))

        # Verify location user has access if being changed
        if 'location_id' in data:
            if not LocationManager().is_allowed(user, [data['location_id']]):
                raise PermissionDenied(_("unauthorized.location"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        from individual.models import PmtConfig
        from individual.services import PmtConfigService

        service = PmtConfigService(user)
        result = service.update(data)
        return result if not result.get('success') else None

    class Input(UpdatePmtConfigInputType):
        pass


class DeletePmtConfigMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    """
    Delete a PMT Configuration.
    """
    _mutation_class = "DeletePmtConfigMutation"
    _mutation_module = "individual"
    _model = None  # Will be set in _mutate

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(IndividualConfig.gql_pmt_rerun_perms):
            raise PermissionDenied(_("unauthorized"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        from individual.models import PmtConfig
        from individual.services import PmtConfigService

        service = PmtConfigService(user)

        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for config_id in ids:
                    service.delete({'id': config_id})

    class Input(OpenIMISMutation.Input):
        ids = graphene.List(graphene.UUID)


class CreatePmtEnrollmentInputType(OpenIMISMutation.Input):
    group_id = graphene.UUID(required=True)
    pmt_class = graphene.String(required=True)
    pmt_score = graphene.Float(required=True)
    status = graphene.String(required=False, default_value="PENDING")
    enrollment_date = graphene.DateTime(required=False)
    beneficiary_id = graphene.Int(required=False)
    json_ext = graphene.types.json.JSONString(required=False)


class UpdatePmtEnrollmentInputType(OpenIMISMutation.Input):
    id = graphene.UUID(required=True)
    group_id = graphene.UUID(required=False)
    pmt_class = graphene.String(required=False)
    pmt_score = graphene.Float(required=False)
    status = graphene.String(required=False)
    enrollment_date = graphene.DateTime(required=False)
    beneficiary_id = graphene.Int(required=False)
    json_ext = graphene.types.json.JSONString(required=False)


class CreatePmtEnrollmentMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    """
    Create a new PMT Enrollment record for a household.
    """
    _mutation_class = "CreatePmtEnrollmentMutation"
    _mutation_module = "individual"
    _model = None  # Will be set in _mutate

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(IndividualConfig.gql_pmt_rerun_perms):
            raise PermissionDenied(_("unauthorized"))

        # Verify group exists and user has access
        if 'group_id' in data:
            from individual.models import Group
            try:
                group = Group.objects.get(id=data['group_id'])
                if group.location_id and not LocationManager().is_allowed(user, [group.location_id]):
                    raise PermissionDenied(_("unauthorized.location"))
            except Group.DoesNotExist:
                raise ValidationError(_("Group not found"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        from individual.models import PmtEnrollment
        from individual.services import PmtEnrollmentService

        service = PmtEnrollmentService(user)
        result = service.create(data)
        return result if not result.get('success') else None

    class Input(CreatePmtEnrollmentInputType):
        pass


class UpdatePmtEnrollmentMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    """
    Update an existing PMT Enrollment record.
    """
    _mutation_class = "UpdatePmtEnrollmentMutation"
    _mutation_module = "individual"
    _model = None  # Will be set in _mutate

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(IndividualConfig.gql_pmt_rerun_perms):
            raise PermissionDenied(_("unauthorized"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        from individual.models import PmtEnrollment
        from individual.services import PmtEnrollmentService

        service = PmtEnrollmentService(user)
        result = service.update(data)
        return result if not result.get('success') else None

    class Input(UpdatePmtEnrollmentInputType):
        pass


class DisenrollPmtEnrollmentInputType(OpenIMISMutation.Input):
    enrollment_id = graphene.UUID(required=True)
    disenrollment_reason = graphene.String(required=False)


class DisenrollPmtEnrollmentMutation(BaseMutation):
    """
    Disenroll a household from PMT-based enrollment.
    Sets enrollment status to DISENROLLED and records disenrollment date/reason.
    """
    _mutation_class = "DisenrollPmtEnrollmentMutation"
    _mutation_module = "individual"

    ok = graphene.Boolean()
    errors = graphene.List(graphene.String)

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(IndividualConfig.gql_pmt_rerun_perms):
            raise PermissionDenied(_("unauthorized"))

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        from individual.models import PmtEnrollment
        from individual.services import PmtEnrollmentService
        from django.utils import timezone

        service = PmtEnrollmentService(user)

        enrollment_id = data.get('enrollment_id')
        disenrollment_reason = data.get('disenrollment_reason', '')

        try:
            enrollment = PmtEnrollment.objects.get(id=enrollment_id)
            enrollment.status = PmtEnrollment.Status.DISENROLLED
            enrollment.disenrollment_date = timezone.now()
            enrollment.disenrollment_reason = disenrollment_reason
            enrollment.save(user=user)

            return cls(ok=True, errors=[])
        except PmtEnrollment.DoesNotExist:
            return cls(ok=False, errors=["PMT Enrollment not found"])
        except Exception as e:
            logger.error(f"Error disenrolling PMT enrollment {enrollment_id}: {str(e)}", exc_info=True)
            return cls(ok=False, errors=[str(e)])

    class Input(DisenrollPmtEnrollmentInputType):
        pass


class CreateDeduplicationIndividualReviewMutation(OpenIMISMutation):
    """
    Create a deduplication review task for individuals.
    Allows users to identify and merge duplicate individual records.
    """
    _mutation_class = "CreateDeduplicationIndividualReviewMutation"
    _mutation_module = "individual"

    ok = graphene.Boolean()
    errors = graphene.List(graphene.String)

    class Input(OpenIMISMutation.Input):
        summary = graphene.List(graphene.JSONString, required=True)

    @classmethod
    def _validate(cls, info, **input_data):
        """Validate user authentication and permissions."""
        user = info.context.user
        if not user or not user.is_authenticated:
            raise PermissionDenied(_("mutation.authentication_required"))

        if not user.has_perms(IndividualConfig.gql_individual_update_perms):
            raise PermissionDenied(_("unauthorized"))

    @classmethod
    def mutate_and_get_payload(cls, root, info, **input_data):
        """Create deduplication review task."""
        try:
            cls._validate(info, **input_data)

            summary = input_data.get('summary')
            service = CreateDeduplicationIndividualReviewTasksService(info.context.user)
            result = service.create_individual_duplication_tasks(summary)

            if result.get('success', False):
                return cls(ok=True, errors=[])

            # output_exception()/output_result_success() put the reason in
            # 'detail'/'message', not 'errors' - surface it so the UI shows
            # something more useful than "Failed to create task".
            errors = list(result.get('errors') or [])
            detail = result.get('detail') or result.get('message')
            if detail and str(detail) not in errors:
                errors.append(str(detail))
            logger.error("CreateDeduplicationIndividualReviewMutation failed: %s", result)
            return cls(ok=False, errors=errors or ["Failed to create deduplication task"])

        except PermissionDenied as e:
            logger.warning(f"CreateDeduplicationIndividualReviewMutation: Permission denied: {str(e)}")
            return cls(ok=False, errors=[str(e)])
        except Exception as e:
            logger.error(f"CreateDeduplicationIndividualReviewMutation: Unexpected error: {str(e)}", exc_info=True)
            return cls(ok=False, errors=[f"Mutation failed: {str(e)}"])


class UpdatePmtGlobalFormulaInputType(OpenIMISMutation.Input):
    id = graphene.String(required=True)
    formula = graphene.types.json.JSONString(required=True)
    is_active = graphene.Boolean(required=False)


class UpdatePmtGlobalFormulaMutation(BaseMutation):
    """
    Maker side of the global PMT formula. Does NOT write the formula directly:
    it creates a tasks_management approval task via the service's checker mixin,
    so the change only takes effect once a second user approves the task.
    """
    _mutation_class = "UpdatePmtGlobalFormulaMutation"
    _mutation_module = "individual"
    _model = PmtGlobalFormula

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(IndividualConfig.gql_pmt_formula_update_perms):
            raise PermissionDenied(_("unauthorized"))

    @classmethod
    def _mutate(cls, user, **data):
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)

        from individual.pmt_service import PmtGlobalFormulaService
        result = PmtGlobalFormulaService(user).create_update_task(data)
        return result if not result['success'] else None

    class Input(UpdatePmtGlobalFormulaInputType):
        pass
