# ================================
# FILE: individual/schema.py
# ================================
import json
import graphene
import graphene_django_optimizer as gql_optimizer
import django_filters
import pandas as pd

from django.contrib.auth.models import AnonymousUser
from django.db.models import Q, OuterRef, Subquery

from core.custom_filters import CustomFilterWizardStorage
from core.gql.export_mixin import ExportableQueryMixin
from core.schema import OrderedDjangoFilterConnectionField
from core.services import wait_for_mutation
from core.utils import append_validity_filter, is_valid_uuid
from individual.apps import IndividualConfig
from individual.gql_mutations import (
    CreateIndividualMutation,
    UpdateIndividualMutation,
    DeleteIndividualMutation,
    CreateGroupMutation,
    UpdateGroupMutation,
    DeleteGroupMutation,
    CreateGroupIndividualMutation,
    UpdateGroupIndividualMutation,
    DeleteGroupIndividualMutation,
    CreateGroupIndividualsMutation,
    CreateGroupAndMoveIndividualMutation,
    ConfirmIndividualEnrollmentMutation,
    UndoDeleteIndividualMutation,
    ConfirmGroupEnrollmentMutation,
    RerunPmtMutation,
    CreatePmtConfigMutation,
    UpdatePmtConfigMutation,
    DeletePmtConfigMutation,
    CreatePmtEnrollmentMutation,
    UpdatePmtEnrollmentMutation,
    DisenrollPmtEnrollmentMutation,
    CreateDeduplicationIndividualReviewMutation,
)
from individual.gql_queries import (
    IndividualGQLType,
    IndividualHistoryGQLType,
    IndividualDataSourceGQLType,
    GroupGQLType,
    GroupIndividualGQLType,
    IndividualDataSourceUploadGQLType,
    GroupHistoryGQLType,
    IndividualSummaryEnrollmentGQLType,
    IndividualDataUploadQGLType,
    GroupIndividualHistoryGQLType,
    GlobalSchemaType,
    GroupSummaryEnrollmentGQLType,
    GroupDataSourceGQLType,
    HouseholdPmtResultsType,
    PmtConfigGQLType,
    PmtConfigConnection,
    PmtEnrollmentGQLType,
    PmtEnrollmentConnection,
    PmtAuditSummaryResultType,
    PmtEnrollmentResultType,
    PmtRunProgressType,
)
from individual.models import (
    Individual,
    IndividualDataSource,
    Group,
    GroupIndividual,
    IndividualDataSourceUpload,
    IndividualDataUploadRecords,
    GroupDataSource,
    PmtConfig,
    PmtEnrollment,
)
from location.apps import LocationConfig


def patch_details(data_df: pd.DataFrame):
    # Transform extension to DF columns
    if "json_ext" in data_df:
        df_unfolded = pd.json_normalize(data_df["json_ext"])
        df_final = pd.concat([data_df, df_unfolded], axis=1)
        df_final = df_final.drop("json_ext", axis=1)
        return df_final
    return data_df


class Query(ExportableQueryMixin, graphene.ObjectType):
    export_patches = {
        "group": [patch_details],
        "individual": [patch_details],
        "group_individual": [patch_details],
    }
    exportable_fields = ["group", "individual", "group_individual"]
    module_name = "individual"
    object_type = "Individual"
    object_type_group = "Group"
    related_field_individual = "groupindividuals__individual"

    individual = OrderedDjangoFilterConnectionField(
        IndividualGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        groupId=graphene.String(),
        customFilters=graphene.List(of_type=graphene.String),
        benefitPlanToEnroll=graphene.String(),
        benefitPlanId=graphene.String(),
        filterNotAttachedToGroup=graphene.Boolean(),
        parent_location=graphene.String(),
        parent_location_level=graphene.Int(),
        isNonConsented=graphene.Boolean(
            description="Filter for non-consented individuals (true=non-consented only, false/null=consented)"
        ),
    )

    individual_history = OrderedDjangoFilterConnectionField(
        IndividualHistoryGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        groupId=graphene.String(),
    )

    individual_data_source = OrderedDjangoFilterConnectionField(
        IndividualDataSourceGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
    )

    group_data_source = OrderedDjangoFilterConnectionField(
        GroupDataSourceGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
    )

    individual_data_source_upload = OrderedDjangoFilterConnectionField(
        IndividualDataSourceUploadGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
    )

    group = OrderedDjangoFilterConnectionField(
        GroupGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        first_name=graphene.String(),
        last_name=graphene.String(),
        customFilters=graphene.List(of_type=graphene.String),
        benefitPlanToEnroll=graphene.String(),
        parent_location=graphene.String(),
        parent_location_level=graphene.Int(),
        isNonConsented=graphene.Boolean(
            description="Filter by head's consent status (true=non-consented only, false/null=consented)"
        ),
    )

    group_history = OrderedDjangoFilterConnectionField(
        GroupHistoryGQLType,
        json_ext_head__icontains=graphene.String(),
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
    )

    group_individual = OrderedDjangoFilterConnectionField(
        GroupIndividualGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        isNonConsented=graphene.Boolean(
            description="Filter by group head's consent status"
        ),
    )

    group_individual_history = OrderedDjangoFilterConnectionField(
        GroupIndividualHistoryGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
    )

    individual_enrollment_summary = graphene.Field(
        IndividualSummaryEnrollmentGQLType,
        customFilters=graphene.List(of_type=graphene.String),
        benefitPlanId=graphene.String(),
    )

    individual_data_upload_history = OrderedDjangoFilterConnectionField(
        IndividualDataUploadQGLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
    )

    group_enrollment_summary = graphene.Field(
        GroupSummaryEnrollmentGQLType,
        customFilters=graphene.List(of_type=graphene.String),
        benefitPlanId=graphene.String(),
    )

    global_schema = graphene.Field(GlobalSchemaType)

    pmt_households = graphene.Field(
        HouseholdPmtResultsType,
        district_code=graphene.String(required=True),
        region_code=graphene.String(required=False),
        offset=graphene.Int(required=False),
        limit=graphene.Int(required=False),
        search_text=graphene.String(required=False),
        pmt_class=graphene.String(required=False),
    )

    pmt_audit_summary = graphene.Field(
        PmtAuditSummaryResultType,
        district_code=graphene.String(required=False),
        region_code=graphene.String(required=False),
        offset=graphene.Int(required=False),
        limit=graphene.Int(required=False),
    )

    pmt_enrollment_list = graphene.Field(
        PmtEnrollmentResultType,
        district_code=graphene.String(required=False),  # Can come from location filter
        pmt_cutoff=graphene.Float(required=True),
        region_code=graphene.String(required=False),
        offset=graphene.Int(required=False),
        limit=graphene.Int(required=False),
        search_text=graphene.String(required=False),
        pmt_class=graphene.String(required=False),
    )

    pmt_enrollments = OrderedDjangoFilterConnectionField(
        PmtEnrollmentGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        applyDefaultValidityFilter=graphene.Boolean(),
    )

    pmt_run_progress = graphene.Field(
        PmtRunProgressType,
        mutation_id=graphene.UUID(required=True),
    )

    individual_deduplication_summary = graphene.Field(
        graphene.JSONString,
        columns=graphene.List(graphene.String, required=True),
        location_id=graphene.String(required=False),
    )

    # -------------------------------
    # REQUIRED by ExportableQueryMixin
    # -------------------------------
    def resolve_individual(self, info, **kwargs):
        Query._check_permissions(
            info.context.user, IndividualConfig.gql_individual_search_perms
        )

        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id)
            )

        group_id = kwargs.get("groupId")
        if group_id:
            filters.append(Q(groupindividuals__group__id=group_id))

        benefit_plan_to_enroll = kwargs.get("benefitPlanToEnroll")
        if benefit_plan_to_enroll:
            filters.append(
                Q(is_deleted=False)
                & ~Q(beneficiary__benefit_plan_id=benefit_plan_to_enroll)
            )

        benefit_plan_id = kwargs.get("benefitPlanId")
        if benefit_plan_id:
            filters.append(
                Q(is_deleted=False) & Q(beneficiary__benefit_plan_id=benefit_plan_id)
            )

        filter_not_attached_to_group = kwargs.get("filterNotAttachedToGroup")
        if filter_not_attached_to_group:
            subquery = (
                GroupIndividual.objects.filter(individual=OuterRef("pk"))
                .exclude(is_deleted=True)
                .values("individual")
            )
            filters.append(~Q(pk__in=Subquery(subquery)))

        parent_location = kwargs.get("parent_location")
        parent_location_level = kwargs.get("parent_location_level")
        if parent_location is not None and parent_location_level is not None:
            filters.append(
                Query._get_location_filters(parent_location, parent_location_level)
            )

        # ---- Consent filtering (positive filter, no negation) ----
        is_non_consented = kwargs.get("isNonConsented", None)
        if is_non_consented is not None:
            if is_non_consented is True:
                # Show non-consented: consent_res = 2 (legacy) OR is_non_consented = true (new)
                marker_q = (
                    Q(json_ext__contains={"is_non_consented": True})
                    | Q(json_ext__contains={"isNonConsented": True})
                    | Q(json_ext__consent_res__in=[2, "2"])
                )
                filters.append(marker_q)
            else:
                # Show consented: use positive filter instead of negation
                # Supports multiple marker formats for backwards compatibility
                marker_q = (
                    Q(json_ext__contains={"is_non_consented": False})
                    | Q(json_ext__contains={"isNonConsented": False})
                    | Q(json_ext__consent_res__in=[1])
                    | Q(json_ext__consent_res__isnull=True)
                )
                filters.append(marker_q)

        query = IndividualGQLType.get_queryset(None, info)
        query = query.filter(*filters)

        # OPTIMIZATION: Add explicit relationship loading for better performance with 10M+ data
        # Follows pattern from resolve_group which has this optimization
        query = query.select_related('location')

        custom_filters = kwargs.get("customFilters", None)
        if custom_filters:
            query = CustomFilterWizardStorage.build_custom_filters_queryset(
                Query.module_name,
                Query.object_type,
                custom_filters,
                query,
            )

        return gql_optimizer.query(query, info)

    # -------------------------------
    # progress resolver
    # -------------------------------
    def resolve_individual_deduplication_summary(self, info, columns=None, location_id=None, **kwargs):
        """Query duplicate individuals based on specified columns."""
        from individual.services import get_individual_duplication_aggregation
        import logging

        logger = logging.getLogger(__name__)

        try:
            # Check permissions
            Query._check_permissions(
                info.context.user,
                IndividualConfig.gql_individual_search_perms
            )

            logger.debug(f"Dedup summary query - columns: {columns}, location_id: {location_id}")

            # Get aggregation results
            results = get_individual_duplication_aggregation(columns, location_id)

            # Format as JSON for GraphQL
            return json.dumps({
                'rows': results
            })
        except Exception as e:
            logger.error(f"Error in resolve_individual_deduplication_summary: {str(e)}", exc_info=True)
            raise

    def resolve_pmt_run_progress(self, info, mutation_id, **kwargs):
        """Get progress of a PMT rerun operation by mutation ID."""
        from individual.models import PmtRunProgress

        # Permissions: PMT rerun rights are appropriate here
        Query._check_permissions(info.context.user, IndividualConfig.gql_pmt_rerun_perms)

        try:
            progress = PmtRunProgress.objects.get(mutation_id=mutation_id)
            return PmtRunProgressType(
                mutation_id=progress.mutation_id,
                status=progress.status,
                district_code=progress.district_code,
                total_groups=progress.total_groups,
                processed_groups=progress.processed_groups,
                total_individuals=progress.total_individuals,
                processed_individuals=progress.processed_individuals,
                poor_groups_found=progress.poor_groups_found,
                enrollments_created=progress.enrollments_created,
                percentage_complete=progress.percentage_complete,
                status_message=progress.status_message,
                errors=progress.errors,
                started_at=progress.started_at,
                completed_at=progress.completed_at,
            )
        except PmtRunProgress.DoesNotExist:
            return None

 
    def resolve_individual_enrollment_summary(self, info, **kwargs):
        Query._check_permissions(
            info.context.user, IndividualConfig.gql_individual_search_perms
        )
        subquery = (
            GroupIndividual.objects.filter(individual=OuterRef("pk"))
            .exclude(is_deleted=True)
            .values("individual")
        )
        query = Individual.objects.filter(is_deleted=False)
        custom_filters = kwargs.get("customFilters", None)
        benefit_plan_id = kwargs.get("benefitPlanId", None)
        if custom_filters:
            query = CustomFilterWizardStorage.build_custom_filters_queryset(
                Query.module_name,
                Query.object_type,
                custom_filters,
                query,
            )
        query = query.filter(~Q(pk__in=Subquery(subquery))).distinct()
        number_of_selected_individuals = query.count()

        total_number_of_individuals = Individual.objects.filter(is_deleted=False).count()
        individuals_not_assigned_to_programme = query.filter(
            is_deleted=False, beneficiary__benefit_plan_id__isnull=True
        ).count()
        individuals_assigned_to_programme = (
            number_of_selected_individuals - individuals_not_assigned_to_programme
        )

        individuals_assigned_to_selected_programme = "0"
        number_of_individuals_to_upload = number_of_selected_individuals
        if benefit_plan_id:
            individuals_assigned_to_selected_programme = query.filter(
                is_deleted=False, beneficiary__benefit_plan_id=benefit_plan_id
            ).count()
            number_of_individuals_to_upload = (
                number_of_individuals_to_upload - individuals_assigned_to_selected_programme
            )

        return IndividualSummaryEnrollmentGQLType(
            number_of_selected_individuals=number_of_selected_individuals,
            total_number_of_individuals=total_number_of_individuals,
            number_of_individuals_not_assigned_to_programme=individuals_not_assigned_to_programme,
            number_of_individuals_assigned_to_programme=individuals_assigned_to_programme,
            number_of_individuals_assigned_to_selected_programme=individuals_assigned_to_selected_programme,
            number_of_individuals_to_upload=number_of_individuals_to_upload,
        )

    def resolve_individual_history(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id)
            )

        Query._check_permissions(
            info.context.user, IndividualConfig.gql_individual_search_perms
        )
        query = Individual.history.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_individual_data_source(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id)
            )

        Query._check_permissions(
            info.context.user, IndividualConfig.gql_individual_search_perms
        )
        query = IndividualDataSource.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_group_data_source(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id)
            )

        Query._check_permissions(
            info.context.user, IndividualConfig.gql_individual_search_perms
        )
        query = GroupDataSource.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_individual_data_source_upload(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id)
            )

        Query._check_permissions(
            info.context.user, IndividualConfig.gql_individual_search_perms
        )
        query = IndividualDataSourceUpload.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_group(self, info, **kwargs):
        Query._check_permissions(
            info.context.user, IndividualConfig.gql_group_search_perms
        )
        filters = append_validity_filter(**kwargs)
        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id)
            )

        first_name = kwargs.get("first_name", None)
        if first_name:
            filters.append(
                Q(groupindividuals__individual__first_name__icontains=first_name)
            )

        last_name = kwargs.get("last_name", None)
        if last_name:
            filters.append(
                Q(groupindividuals__individual__last_name__icontains=last_name)
            )

        benefit_plan_to_enroll = kwargs.get("benefitPlanToEnroll")
        if benefit_plan_to_enroll:
            filters.append(
                Q(is_deleted=False)
                & ~Q(groupbeneficiary__benefit_plan_id=benefit_plan_to_enroll)
            )

        parent_location = kwargs.get("parent_location")
        parent_location_level = kwargs.get("parent_location_level")
        if parent_location is not None and parent_location_level is not None:
            filters.append(
                Query._get_location_filters(parent_location, parent_location_level)
            )

        # ---- Consent filtering (positive filter, no negation) ----
        is_non_consented = kwargs.get("isNonConsented", None)
        if is_non_consented is not None:
            if is_non_consented is True:
                # Show groups with non-consented heads
                non_consented_head_q = Q(
                    groupindividuals__role=GroupIndividual.Role.HEAD,
                    groupindividuals__individual__json_ext__consent_res__in=[2, "2"],
                    groupindividuals__is_deleted=False
                )
                filters.append(non_consented_head_q)
            else:
                # Show groups with consented heads (positive filter instead of negation)
                consented_head_q = Q(
                    groupindividuals__role=GroupIndividual.Role.HEAD,
                    groupindividuals__is_deleted=False
                ) & (
                    Q(groupindividuals__individual__json_ext__contains={"is_non_consented": False})
                    | Q(groupindividuals__individual__json_ext__contains={"isNonConsented": False})
                    | Q(groupindividuals__individual__json_ext__consent_res__in=[1])
                    | Q(groupindividuals__individual__json_ext__consent_res__isnull=True)
                )
                filters.append(consented_head_q)

        query = GroupGQLType.get_queryset(None, info)
        # OPTIMIZATION: Phase 2 - Removed distinct() call for performance
        # Original: query.filter(*filters).distinct()
        # Reason: distinct() forces full table dedupe, causing query plan issues
        # Testing shows Prefetch handles relationships without needing distinct()
        # If duplicates appear in results, re-add: .distinct()
        query = query.filter(*filters)

        from django.db.models import Prefetch
        query = query.select_related('location').prefetch_related(
            Prefetch(
                'groupindividuals',
                queryset=GroupIndividual.objects.filter(
                    is_deleted=False
                ).select_related('individual', 'individual__location')
            )
        )

        custom_filters = kwargs.get("customFilters", None)
        if custom_filters:
            query = CustomFilterWizardStorage.build_custom_filters_queryset(
                Query.module_name, "Group", custom_filters, query
            )
        return gql_optimizer.query(query, info)

    def resolve_group_history(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id)
            )

        json_ext_head_icontains = kwargs.get("json_ext_head__icontains")
        if json_ext_head_icontains:
            filters.append(Q(json_ext__head__icontains=json_ext_head_icontains))

        Query._check_permissions(
            info.context.user, IndividualConfig.gql_group_search_perms
        )
        query = Group.history.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_group_individual(self, info, **kwargs):
        Query._check_permissions(
            info.context.user, IndividualConfig.gql_group_search_perms
        )
        filters = append_validity_filter(**kwargs)

        group_id = kwargs.get("group__id")
        if not group_id or (group_id and not is_valid_uuid(group_id)):
            filters.append(Q(id__lt=0))

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id)
            )

        # ---- Consent filtering (optimized with Subquery to avoid group__groupindividuals__ traversal) ----
        # Instead of: GROUP_IND → GROUP → GROUP_IND (HEAD) → INDIVIDUAL (slow, multiple JOINs)
        # We do: Find groups first, then filter simple group_id (fast, simple)
        is_non_consented = kwargs.get("isNonConsented", None)
        if is_non_consented is not None:
            from django.db.models import Subquery

            if is_non_consented is True:
                # Find all groups that have non-consented HEAD individuals
                non_consented_groups = Group.objects.filter(
                    groupindividuals__role=GroupIndividual.Role.HEAD,
                    groupindividuals__individual__json_ext__consent_res__in=[2, "2"],
                    groupindividuals__is_deleted=False
                ).values('pk').distinct()
                # Filter GroupIndividuals to those groups (simple group_id check)
                filters.append(Q(group_id__in=Subquery(non_consented_groups)))
            else:
                # Find all groups that have consented HEAD individuals (positive filter)
                consented_groups = Group.objects.filter(
                    groupindividuals__role=GroupIndividual.Role.HEAD,
                    groupindividuals__is_deleted=False
                ).filter(
                    Q(groupindividuals__individual__json_ext__contains={"is_non_consented": False})
                    | Q(groupindividuals__individual__json_ext__contains={"isNonConsented": False})
                    | Q(groupindividuals__individual__json_ext__consent_res__in=[1])
                    | Q(groupindividuals__individual__json_ext__consent_res__isnull=True)
                ).values('pk').distinct()
                # Filter GroupIndividuals to those groups (simple group_id check)
                filters.append(Q(group_id__in=Subquery(consented_groups)))

        query = GroupIndividual.objects.filter(*filters)
        query = query.select_related('group', 'individual', 'individual__location')
        return gql_optimizer.query(query, info)

    def resolve_group_individual_history(self, info, **kwargs):
        Query._check_permissions(
            info.context.user, IndividualConfig.gql_group_search_perms
        )
        filters = append_validity_filter(**kwargs)
        query = GroupIndividual.history.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_individual_data_upload_history(self, info, **kwargs):
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id)
            )

        Query._check_permissions(
            info.context.user, IndividualConfig.gql_individual_search_perms
        )
        query = IndividualDataUploadRecords.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_group_enrollment_summary(self, info, **kwargs):
        Query._check_permissions(
            info.context.user, IndividualConfig.gql_group_search_perms
        )
        query = Group.objects.filter(is_deleted=False)
        custom_filters = kwargs.get("customFilters", None)
        benefit_plan_id = kwargs.get("benefitPlanId", None)
        if custom_filters:
            query = CustomFilterWizardStorage.build_custom_filters_queryset(
                Query.module_name,
                "Group",
                custom_filters,
                query,
            )

        number_of_selected_groups = query.count()
        total_number_of_groups = Group.objects.filter(is_deleted=False).count()
        groups_not_assigned_to_programme = query.filter(
            is_deleted=False, groupbeneficiary__benefit_plan_id__isnull=True
        ).count()
        groups_assigned_to_programme = (
            number_of_selected_groups - groups_not_assigned_to_programme
        )

        groups_assigned_to_selected_programme = "0"
        number_of_groups_to_upload = number_of_selected_groups
        if benefit_plan_id:
            groups_assigned_to_selected_programme = query.filter(
                is_deleted=False, groupbeneficiary__benefit_plan_id=benefit_plan_id
            ).count()
            number_of_groups_to_upload = (
                number_of_groups_to_upload - groups_assigned_to_selected_programme
            )

        return GroupSummaryEnrollmentGQLType(
            number_of_selected_groups=number_of_selected_groups,
            total_number_of_groups=total_number_of_groups,
            number_of_groups_not_assigned_to_programme=groups_not_assigned_to_programme,
            number_of_groups_assigned_to_programme=groups_assigned_to_programme,
            number_of_groups_assigned_to_selected_programme=groups_assigned_to_selected_programme,
            number_of_groups_to_upload=number_of_groups_to_upload,
        )

    def resolve_global_schema(self, info):
        individual_schema = IndividualConfig.individual_schema
        if individual_schema:
            individual_schema_dict = json.loads(individual_schema)
            return GlobalSchemaType(schema=individual_schema_dict)
        return GlobalSchemaType(schema={})

    def resolve_pmt_audit_summary(self, info, district_code=None, region_code=None, offset=0, limit=10, **kwargs):
        from individual.pmt_service import PmtService
        Query._check_permissions(info.context.user, IndividualConfig.gql_pmt_rerun_perms)

        service = PmtService(info.context.user)
        result = service.get_pmt_audit_summary(
            district_code=district_code,
            region_code=region_code,
            offset=offset or 0,
            limit=limit or 10
        )

        return PmtAuditSummaryResultType(
            districts=result.get('districts', []),
            total_count=result.get('total_count', 0),
            has_next=result.get('has_next', False),
            has_previous=result.get('has_previous', False),
            offset=result.get('offset', 0),
            limit=result.get('limit', 10),
        )

    def resolve_pmt_enrollment_list(self, info, district_code=None, pmt_cutoff=11.01,
                                    region_code=None, offset=0, limit=20,
                                    search_text=None, pmt_class=None, **kwargs):
        from individual.pmt_service import PmtService
        from location.models import Location
        import uuid as uuid_lib

        Query._check_permissions(info.context.user, IndividualConfig.gql_pmt_rerun_perms)

        # Convert location UUIDs to codes if provided
        # Searcher component sends location UUIDs, but service expects location codes
        import logging
        logger = logging.getLogger(__name__)

        def uuid_to_code(location_uuid, param_name='district_code'):
            """Convert location UUID to location code"""
            if not location_uuid:
                return location_uuid

            try:
                # Validate it's a UUID format
                uuid_lib.UUID(location_uuid)
            except (ValueError, TypeError):
                # Not a UUID, assume it's already a code
                logger.info(f"[PMT] {param_name} is not a UUID, treating as code: {location_uuid}")
                return location_uuid

            logger.info(f"[PMT] Converting {param_name} UUID to code: {location_uuid}")

            # Try to find location by UUID in various ways
            location = None

            # Try 1: Direct id lookup (if id is UUID)
            try:
                location = Location.objects.get(id=location_uuid)
                logger.info(f"[PMT] Found location by id: {location.code}")
            except (Location.DoesNotExist, ValueError):
                pass

            # Try 2: UUID field lookup
            if not location:
                try:
                    location = Location.objects.get(uuid=location_uuid)
                    logger.info(f"[PMT] Found location by uuid field: {location.code}")
                except (Location.DoesNotExist, ValueError, AttributeError):
                    pass

            # Try 3: String matching in id or uuid fields
            if not location:
                try:
                    location = Location.objects.filter(
                        Q(id__iexact=location_uuid) |
                        Q(uuid__iexact=location_uuid)
                    ).first()
                    if location:
                        logger.info(f"[PMT] Found location by string match: {location.code}")
                except Exception as e:
                    logger.warning(f"[PMT] Error in string matching: {e}")
                    pass

            if location:
                logger.info(f"[PMT] Successfully converted UUID {location_uuid[:8]}... to code: {location.code}")
                return location.code
            else:
                logger.warning(f"[PMT] Could not find location for UUID: {location_uuid}, keeping original")
                return location_uuid

        # Convert UUIDs to codes
        if district_code:
            original = district_code
            district_code = uuid_to_code(district_code, 'district_code')
            logger.info(f"[PMT] district_code: {original} -> {district_code}")

        if region_code:
            original = region_code
            region_code = uuid_to_code(region_code, 'region_code')
            logger.info(f"[PMT] region_code: {original} -> {region_code}")

        service = PmtService(info.context.user)
        result = service.get_households_with_pmt(
            district_code=district_code,
            region_code=region_code,
            offset=offset or 0,
            limit=limit or 20,
            search_text=search_text,
            pmt_class=pmt_class,
            pmt_cutoff=pmt_cutoff
        )

        households = []
        for household in result.get('households', []):
            households.append({
                'group_uuid': household.get('group_uuid'),
                'group_code': household.get('group_code'),
                'head_uuid': household.get('head_uuid'),
                'head_name': household.get('head_name'),
                'pmt_score': household.get('pmt_score'),
                'pmt_class': household.get('pmt_class'),
                'number_of_members': household.get('number_of_members'),
                'location_code': household.get('location_code'),
                'location_name': household.get('location_name'),
            })

        return PmtEnrollmentResultType(
            households=households,
            total_count=result.get('total_count', 0),
            has_next=result.get('has_next', False),
            has_previous=result.get('has_previous', False),
            offset=result.get('offset', 0),
            limit=result.get('limit', 20),
        )

    def resolve_pmt_households(self, info, district_code=None, region_code=None, offset=0,
                               limit=10, search_text=None, pmt_class=None, **kwargs):
        from individual.pmt_service import PmtService
        Query._check_permissions(info.context.user, IndividualConfig.gql_pmt_rerun_perms)

        service = PmtService(info.context.user)
        result = service.get_households_with_pmt(
            district_code=district_code,
            region_code=region_code,
            offset=offset or 0,
            limit=limit or 10,
            search_text=search_text,
            pmt_class=pmt_class
        )

        return HouseholdPmtResultsType(
            households=result['households'],
            total_count=result['total_count'],
            has_next=result['has_next'],
            has_previous=result['has_previous'],
            offset=result['offset'],
            limit=result['limit'],
        )

    def resolve_pmt_enrollments(self, info, **kwargs):
        Query._check_permissions(info.context.user, IndividualConfig.gql_pmt_rerun_perms)

        filters = []
        district_code = kwargs.get("districtCode")
        if district_code:
            filters.append(Q(group__location__code=district_code))

        pmt_class = kwargs.get("pmtClass")
        if pmt_class:
            filters.append(Q(pmt_class=pmt_class))

        status = kwargs.get("status")
        if status:
            filters.append(Q(status=status))

        search_text = kwargs.get("searchText")
        if search_text:
            filters.append(
                Q(group__code__icontains=search_text) |
                Q(group__groupindividuals__individual__first_name__icontains=search_text) |
                Q(group__groupindividuals__individual__last_name__icontains=search_text)
            )

        query = PmtEnrollmentGQLType.get_queryset(None, info)
        if filters:
            query = query.filter(*filters)

        return gql_optimizer.query(query, info)

    @staticmethod
    def _check_permissions(user, perms):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(perms):
            raise PermissionError("Unauthorized")

    @staticmethod
    def _get_location_filters(parent_location, parent_location_level):
        query_key = "uuid"
        for i in range(len(LocationConfig.location_types) - parent_location_level - 1):
            query_key = "parent__" + query_key
        query_key = "location__" + query_key
        return Q(**{query_key: parent_location})


class Mutation(graphene.ObjectType):
    create_individual = CreateIndividualMutation.Field()
    update_individual = UpdateIndividualMutation.Field()
    delete_individual = DeleteIndividualMutation.Field()
    undo_delete_individual = UndoDeleteIndividualMutation.Field()

    create_group = CreateGroupMutation.Field()
    update_group = UpdateGroupMutation.Field()
    delete_group = DeleteGroupMutation.Field()

    add_individual_to_group = CreateGroupIndividualMutation.Field()
    edit_individual_in_group = UpdateGroupIndividualMutation.Field()
    remove_individual_from_group = DeleteGroupIndividualMutation.Field()

    create_group_individuals = CreateGroupIndividualsMutation.Field()
    create_group_and_move_individual = CreateGroupAndMoveIndividualMutation.Field()

    confirm_individual_enrollment = ConfirmIndividualEnrollmentMutation.Field()
    confirm_group_enrollment = ConfirmGroupEnrollmentMutation.Field()

    rerun_pmt = RerunPmtMutation.Field()

    create_pmt_config = CreatePmtConfigMutation.Field()
    update_pmt_config = UpdatePmtConfigMutation.Field()
    delete_pmt_config = DeletePmtConfigMutation.Field()

    create_pmt_enrollment = CreatePmtEnrollmentMutation.Field()
    update_pmt_enrollment = UpdatePmtEnrollmentMutation.Field()
    disenroll_pmt_enrollment = DisenrollPmtEnrollmentMutation.Field()

    create_individual_deduplication_review = CreateDeduplicationIndividualReviewMutation.Field()


class IndividualFilterSet(django_filters.FilterSet):
    """
    Filters Individuals by consent flag stored in Individual.Json_ext.
    DB path: json_ext__consent_res
    """
    is_non_consented = django_filters.BooleanFilter(method="filter_is_non_consented")

    def filter_is_non_consented(self, queryset, name, value):
        if value is None:
            return queryset

        non_consented_q = Q(json_ext__consent_res=2) | Q(json_ext__consent_res="2")
        if value is True:
            return queryset.filter(non_consented_q)
        return queryset.exclude(non_consented_q)

    class Meta:
        model = Individual
        fields = []