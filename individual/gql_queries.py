import graphene
import django_filters
from django.db.models import Q
from django.contrib.auth.models import AnonymousUser
from graphene_django import DjangoObjectType
import graphene_django_optimizer as gql_optimizer

from core import prefix_filterset, ExtendedConnection
from core.gql_queries import UserGQLType
from individual.apps import IndividualConfig
from individual.models import Individual, IndividualDataSource, Group, GroupIndividual, \
    IndividualDataSourceUpload, IndividualDataUploadRecords, GroupDataSource, PmtConfig, PmtEnrollment, PmtRunProgress


def _have_permissions(user, permission):
    if isinstance(user, AnonymousUser):
        return False
    if not user.id:
        return False
    return user.has_perms(permission)


class JsonExtMixin:
    def resolve_json_ext(self, info):
        if _have_permissions(info.context.user, IndividualConfig.gql_individual_search_perms):
            return self.json_ext
        return None


class IndividualFilterSet(django_filters.FilterSet):
    """
    Adds GraphQL filter: isNonConsented (camelCase) 

    IMPORTANT:
    - This is ETL-safe: no DB migration required.
    - It relies on json_ext markers. We support several common keys so older/newer payloads still work.
    - Best practice going forward: ETL should set json_ext["is_non_consented"] = true
      (or json_ext["raw"]["is_non_consented"] = true) so this filter always works.
    """

    is_non_consented = django_filters.BooleanFilter(method="filter_is_non_consented")

    def filter_is_non_consented(self, queryset, name, value):
        if value is None:
            return queryset

        # Supported marker patterns (top-level + raw)
        marker_q = (
            # Preferred explicit marker (future-proof if ETL sets it later)
            Q(json_ext__contains={"is_non_consented": True})
            | Q(json_ext__contains={"isNonConsented": True})

            # real storage: json_ext.consent_res (flat structure)
            | Q(json_ext__consent_res=2)
            | Q(json_ext__consent_res="2")
        )



        if value is True:
            return queryset.filter(marker_q)

        # value is False => exclude those marked as non-consented
        return queryset.exclude(marker_q)

    class Meta:
        model = Individual
        fields = []

class IndividualGQLType(DjangoObjectType):
    uuid = graphene.String(source="uuid")

    # GraphQL camelCase fields for UI
    tf4_no = graphene.String(name="tf4No")
    interview_key = graphene.String(name="interviewKey")

    class Meta:
        model = Individual
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact", "isnull"],
            "first_name": ["iexact", "istartswith", "icontains"],
            "last_name": ["iexact", "istartswith", "icontains"],
            "dob": ["exact", "lt", "lte", "gt", "gte"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "location": ["isnull"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return Individual.get_queryset(queryset, info.context.user)

    def resolve_tf4_no(self, info):
        # NOTE: here "self" is the Django model instance (Individual)
        if not _have_permissions(info.context.user, IndividualConfig.gql_individual_search_perms):
            return None

        ext = getattr(self, "json_ext", None) or {}
        raw = ext.get("raw") or {}  # Flat structure: raw is at top level of json_ext

        v = raw.get("TF4_NO") or raw.get("tf4_no")
        return str(v) if v is not None else None

    def resolve_interview_key(self, info):
        """
        You said: external_id = interview_key (configured in module config)
        So UI "interviewKey" should show external_id (e.g. 33-16-67-10)
        """
        if not _have_permissions(info.context.user, IndividualConfig.gql_individual_search_perms):
            return None

        ext = getattr(self, "json_ext", None) or {}
        raw = ext.get("raw") or {}  # Flat structure

        v = (
            ext.get("external_id")
            or ext.get("interview_key")
            or ext.get("interviewKey")
            or raw.get("external_id")
            or raw.get("interview_key")
            or raw.get("interviewKey")
        )

        return str(v) if v is not None else None


    @classmethod
    def get_queryset(cls, queryset, info):
        return Individual.get_queryset(queryset, info.context.user)

class IndividualHistoryGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    user_updated = graphene.Field(UserGQLType)

    def resolve_user_updated(self, info):
        return self.user_updated

    class Meta:
        model = Individual.history.model
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "first_name": ["iexact", "istartswith", "icontains"],
            "last_name": ["iexact", "istartswith", "icontains"],
            "dob": ["exact", "lt", "lte", "gt", "gte"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("user_updated__", UserGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        accessible_individual_query = Individual.get_queryset(None, info.context.user)
        accessible_individuals = gql_optimizer.query(accessible_individual_query, info)
        accessible_uuids = set(accessible_individuals.values_list('uuid', flat=True))
        return queryset.filter(id__in=accessible_uuids)


class IndividualDataSourceUploadGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = IndividualDataSourceUpload
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "status": ["iexact", "istartswith", "icontains"],
            "source_type": ["iexact", "istartswith", "icontains"],
            "source_name": ["iexact", "istartswith", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class IndividualDataSourceGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = IndividualDataSource
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact", "isnull"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),
            **prefix_filterset("upload__", IndividualDataSourceUploadGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class GroupGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    head = graphene.Field(IndividualGQLType)

    def resolve_head(self, info):
        """
        Resolve the head individual of this group.
        Applies consent filtering for consistency with resolve_group().
        """
        # Get the isNonConsented parameter from parent query context
        is_non_consented = False
        try:
            if hasattr(info, 'variable_values') and info.variable_values:
                is_non_consented = info.variable_values.get("isNonConsented", False)
        except Exception:
            pass

        queryset = Individual.objects.filter(
            groupindividuals__group__id=self.id,
            groupindividuals__role=GroupIndividual.Role.HEAD,
            groupindividuals__is_deleted=False,
        )

        # Apply consent filtering (exclude non-consented by default)
        if not is_non_consented:
            non_consented_q = (
                Q(json_ext__consent_res=2) | Q(json_ext__consent_res="2")
            )
            queryset = queryset.exclude(non_consented_q)

        # Performance optimization
        queryset = queryset.select_related('location')

        return queryset.first()

    class Meta:
        model = Group
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact", "isnull"],
            "code": ["iexact", "istartswith", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "location": ["isnull"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return Group.get_queryset(queryset, info.context.user)


class GroupHistoryGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    user_updated = graphene.Field(UserGQLType)
    head = graphene.Field(IndividualGQLType)

    def resolve_head(self, info):
        """
        Resolve the head individual of this group (historical version).
        Applies consent filtering for consistency with resolve_group().
        """
        # Get the isNonConsented parameter from parent query context
        is_non_consented = False
        try:
            if hasattr(info, 'variable_values') and info.variable_values:
                is_non_consented = info.variable_values.get("isNonConsented", False)
        except Exception:
            pass

        queryset = Individual.objects.filter(
            groupindividuals__group__id=self.id,
            groupindividuals__role=GroupIndividual.Role.HEAD,
            groupindividuals__is_deleted=False,
        )

        # Apply consent filtering (exclude non-consented by default)
        if not is_non_consented:
            non_consented_q = (
                Q(json_ext__consent_res=2) | Q(json_ext__consent_res="2")
            )
            queryset = queryset.exclude(non_consented_q)

        # Performance optimization
        queryset = queryset.select_related('location')

        return queryset.first()

    def resolve_user_updated(self, info):
        return self.user_updated

    class Meta:
        model = Group.history.model
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("user_updated__", UserGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        accessible_group_query = Group.get_queryset(None, info.context.user)
        accessible_groups = gql_optimizer.query(accessible_group_query, info)
        accessible_uuids = set(accessible_groups.values_list('uuid', flat=True))
        return queryset.filter(id__in=accessible_uuids)


class GroupIndividualGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = GroupIndividual
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "role": ["exact", "iexact", "istartswith", "icontains"],
            "recipient_type": ["exact", "iexact", "istartswith", "icontains"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),
            **prefix_filterset("group__", GroupGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return GroupIndividual.get_queryset(queryset, info.context.user)


class GroupIndividualHistoryGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    user_updated = graphene.Field(UserGQLType)

    def resolve_user_updated(self, info):
        return self.user_updated

    class Meta:
        model = GroupIndividual.history.model
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "role": ["exact", "iexact", "istartswith", "icontains"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),
            **prefix_filterset("group__", GroupGQLType._meta.filter_fields),
            **prefix_filterset("user_updated__", UserGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        accessible_group_query = Group.get_queryset(None, info.context.user)
        accessible_groups = gql_optimizer.query(accessible_group_query, info)
        accessible_uuids = set(accessible_groups.values_list('uuid', flat=True))
        return queryset.filter(group__id__in=accessible_uuids)


class IndividualDataUploadQGLType(DjangoObjectType, JsonExtMixin):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = IndividualDataUploadRecords
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            "workflow": ["exact", "iexact", "startswith", "istartswith", "contains", "icontains"],
            **prefix_filterset("data_upload__", IndividualDataSourceUploadGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class GroupDataSourceGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = GroupDataSource
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact", "isnull"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("group__", GroupGQLType._meta.filter_fields),
            **prefix_filterset("upload__", IndividualDataSourceUploadGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class IndividualSummaryEnrollmentGQLType(graphene.ObjectType):
    number_of_selected_individuals = graphene.String()
    total_number_of_individuals = graphene.String()
    number_of_individuals_not_assigned_to_programme = graphene.String()
    number_of_individuals_assigned_to_programme = graphene.String()
    number_of_individuals_assigned_to_selected_programme = graphene.String()
    number_of_individuals_to_upload = graphene.String()


class GroupSummaryEnrollmentGQLType(graphene.ObjectType):
    number_of_selected_groups = graphene.String()
    total_number_of_groups = graphene.String()
    number_of_groups_not_assigned_to_programme = graphene.String()
    number_of_groups_assigned_to_programme = graphene.String()
    number_of_groups_assigned_to_selected_programme = graphene.String()
    number_of_groups_to_upload = graphene.String()


class GlobalSchemaType(graphene.ObjectType):
    schema = graphene.JSONString()


class HouseholdPmtResultType(graphene.ObjectType):
    """Result type for household PMT data."""
    group_uuid = graphene.String()
    group_code = graphene.String()
    head_uuid = graphene.String()
    head_name = graphene.String()
    pmt_score = graphene.Float()
    pmt_class = graphene.String()
    number_of_members = graphene.Int()
    location_code = graphene.String()
    location_name = graphene.String()


class HouseholdPmtResultConnection(graphene.relay.Connection):
    """Connection type for paginated household PMT results."""
    total_count = graphene.Int()

    class Meta:
        node = HouseholdPmtResultType


class HouseholdPmtResultsType(graphene.ObjectType):
    """Result type for household PMT query with pagination info."""
    households = graphene.List(HouseholdPmtResultType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()
    has_previous = graphene.Boolean()
    offset = graphene.Int()
    limit = graphene.Int()


class PmtConfigGQLType(DjangoObjectType):
    """GraphQL type for PMT Configuration."""
    uuid = graphene.String(source='uuid')
    location_uuid = graphene.String(source='location.uuid')
    location_name = graphene.String(source='location.name')

    class Meta:
        model = PmtConfig
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "location__id": ["exact"],
            "location__name": ["icontains"],
            "pmt_cutoff": ["exact", "lt", "lte", "gt", "gte"],
            "is_active": ["exact"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return PmtConfig.get_queryset(queryset, info.context.user)


class PmtConfigConnection(graphene.relay.Connection):
    """Connection type for paginated PMT configuration results with relay cursor pagination."""
    total_count = graphene.Int(description="Total number of PMT configurations")

    class Meta:
        node = PmtConfigGQLType


class PmtEnrollmentGQLType(DjangoObjectType):
    """GraphQL type for PMT Enrollment."""
    uuid = graphene.String(source='uuid')
    group_uuid = graphene.String(source='group.uuid')
    group_code = graphene.String(source='group.code')
    head_name = graphene.String()

    def resolve_head_name(self, info):
        """Get the head individual's name for this group."""
        try:
            head = self.group.groupindividuals_set.filter(
                role='HEAD',
                is_deleted=False
            ).first()
            if head and head.individual:
                return f"{head.individual.first_name} {head.individual.last_name}"
        except Exception:
            pass
        return None

    class Meta:
        model = PmtEnrollment
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "group__id": ["exact"],
            "group__code": ["icontains"],
            "pmt_class": ["exact"],
            "pmt_score": ["exact", "lt", "lte", "gt", "gte"],
            "status": ["exact"],
            "enrollment_date": ["exact", "lt", "lte", "gt", "gte"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return PmtEnrollment.get_queryset(queryset, info.context.user)


class PmtEnrollmentConnection(graphene.relay.Connection):
    """Connection type for paginated PMT enrollment results with relay cursor pagination."""
    total_count = graphene.Int(description="Total number of PMT enrollments")

    class Meta:
        node = PmtEnrollmentGQLType


# Custom types for PMT Audit Summary (PMT Configuration Page)
class PmtDistrictSummaryType(graphene.ObjectType):
    """District summary data for PMT audit/configuration view."""
    district_code = graphene.String()
    district_name = graphene.String()
    pmt_cutoff = graphene.Float()
    poor_count = graphene.Int()
    non_poor_count = graphene.Int()


class PmtAuditSummaryResultType(graphene.ObjectType):
    """PMT Audit Summary result with pagination."""
    districts = graphene.List(PmtDistrictSummaryType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()
    has_previous = graphene.Boolean()
    offset = graphene.Int()
    limit = graphene.Int()


# Custom types for PMT Enrollment List (PMT Enrollment Page)
class PmtHouseholdEnrollmentType(graphene.ObjectType):
    """Household enrollment data for PMT enrollment list view."""
    group_uuid = graphene.String()
    group_code = graphene.String()
    head_uuid = graphene.String()
    head_name = graphene.String()
    pmt_score = graphene.Float()
    pmt_class = graphene.String()
    number_of_members = graphene.Int()
    location_code = graphene.String()
    location_name = graphene.String()


class PmtEnrollmentResultType(graphene.ObjectType):
    """PMT Enrollment list result with pagination."""
    households = graphene.List(PmtHouseholdEnrollmentType)
    total_count = graphene.Int()
    has_next = graphene.Boolean()
    has_previous = graphene.Boolean()
    offset = graphene.Int()
    limit = graphene.Int()


# Progress tracking type for PMT Rerun
class PmtRunProgressType(graphene.ObjectType):
    """Progress tracking for PMT rerun operations."""
    mutation_id = graphene.UUID()
    status = graphene.String()
    district_code = graphene.String()
    total_groups = graphene.Int()
    processed_groups = graphene.Int()
    total_individuals = graphene.Int()
    processed_individuals = graphene.Int()
    poor_groups_found = graphene.Int()
    enrollments_created = graphene.Int()
    percentage_complete = graphene.Int()
    status_message = graphene.String()
    errors = graphene.List(graphene.String)
    started_at = graphene.DateTime()
    completed_at = graphene.DateTime()

