import graphene
import graphene_django_optimizer as gql_optimizer
from django.forms import FloatField
from sqlalchemy import Cast
from django.contrib.auth.models import AnonymousUser
from graphene_django import DjangoObjectType
from django.db.models.fields.json import KeyTextTransform
from core import prefix_filterset, ExtendedConnection
from core.gql_queries import UserGQLType
from individual.apps import IndividualConfig
from individual.models import Individual, IndividualDataSource, Group, GroupIndividual, \
    IndividualDataSourceUpload, IndividualDataUploadRecords, GroupDataSource


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


class IndividualGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

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
        return Individual.objects.filter(
            groupindividuals__group__id=self.id,
            groupindividuals__role=GroupIndividual.Role.HEAD,
            groupindividuals__is_deleted=False,
        ).first()

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
        return Individual.objects.filter(
            groupindividuals__group__id=self.id,
            groupindividuals__role=GroupIndividual.Role.HEAD,
            groupindividuals__is_deleted=False,
        ).first()

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
