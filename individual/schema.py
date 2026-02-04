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
)
from individual.models import (
    Individual,
    IndividualDataSource,
    Group,
    GroupIndividual,
    IndividualDataSourceUpload,
    IndividualDataUploadRecords,
    GroupDataSource,
)
from location.apps import LocationConfig


def patch_details(data_df: pd.DataFrame):
    # Transform extension to DF columns
    if "json_ext" in data_df:
        df_unfolded = pd.json_normalize(data_df["json_ext"])
        # Merge unfolded DataFrame with the original DataFrame
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
            description="Filter for non-consented household stubs"
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

        # ---- Consent filtering (default: EXCLUDE non-consented stubs) ----
        is_non_consented = kwargs.get("isNonConsented", None)

        non_consented_q = (
            Q(json_ext__json_ext__consent_res=2)
            | Q(json_ext__json_ext__consent_res="2")
        )

        if is_non_consented is True:
            # show only non-consented
            filters.append(non_consented_q)
        else:
            # default (and also when explicitly false): hide non-consented
            filters.append(~non_consented_q)

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

        query = IndividualGQLType.get_queryset(None, info)
        query = query.filter(*filters)

        custom_filters = kwargs.get("customFilters", None)
        if custom_filters:
            query = CustomFilterWizardStorage.build_custom_filters_queryset(
                Query.module_name,
                Query.object_type,
                custom_filters,
                query,
            )

        return gql_optimizer.query(query, info)

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
        # Aggregation for selected individuals
        number_of_selected_individuals = query.count()

        # Aggregation for total number of individuals
        total_number_of_individuals = Individual.objects.filter(
            is_deleted=False
        ).count()
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
                number_of_individuals_to_upload
                - individuals_assigned_to_selected_programme
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

        query = GroupGQLType.get_queryset(None, info)
        query = query.filter(*filters).distinct()

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
        if not group_id or group_id and not is_valid_uuid(group_id):
            # it will result in empty query
            filters.append(Q(id__lt=0))

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(
                Q(mutations__mutation__client_mutation_id=client_mutation_id)
            )

        query = GroupIndividual.objects.filter(*filters)
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
        # Aggregation for selected groups
        number_of_selected_groups = query.count()

        # Aggregation for total number of groups
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

class IndividualFilterSet(django_filters.FilterSet):
    """
    Filters Individuals by consent flag stored in Individual.Json_ext.

    IMPORTANT:
    Your DB query shows the real path is:
      Json_ext -> 'json_ext' ->> 'consent_res'
    So the Django ORM JSONField path must be:
      json_ext__json_ext__consent_res
    """

    is_non_consented = django_filters.BooleanFilter(method="filter_is_non_consented")

    def filter_is_non_consented(self, queryset, name, value):
        if value is None:
            return queryset

        non_consented_q = (
            Q(json_ext__json_ext__consent_res=2)
            | Q(json_ext__json_ext__consent_res="2")
        )

        if value is True:
            # only non-consented
            return queryset.filter(non_consented_q)

        # explicitly false -> only consented (anything NOT 2)
        return queryset.exclude(non_consented_q)

    class Meta:
        model = Individual
        fields = []
