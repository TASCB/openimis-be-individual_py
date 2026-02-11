from django.apps import apps
from django.conf import settings

is_unit_test_env = getattr(settings, 'IS_UNIT_TEST_ENV', False)

# Check if the 'opensearch_reports' app is in INSTALLED_APPS
if 'opensearch_reports' in apps.app_configs:
    from opensearch_reports.service import BaseSyncDocument
    from django_opensearch_dsl import fields as opensearch_fields
    from django_opensearch_dsl.registries import registry
    from individual.models import (
        Individual,
        IndividualDataSourceUpload,
        GroupIndividual,
        Group
    )

    # skip indexing on model update when running unit tests to avoid connection issues
    auto_refresh = not is_unit_test_env

    @registry.register_document
    class IndividualDocument(BaseSyncDocument):
        DASHBOARD_NAME = 'Individual'

        first_name = opensearch_fields.KeywordField()
        last_name = opensearch_fields.KeywordField()
        dob = opensearch_fields.DateField()
        date_created = opensearch_fields.DateField()
        json_ext = opensearch_fields.ObjectField()

        class Index:
            name = 'individual'
            settings = {
                'number_of_shards': 1,
                'number_of_replicas': 0
            }
            auto_refresh = auto_refresh

        class Django:
            model = Individual
            fields = [
                'id'
            ]
            queryset_pagination = 5000

        def prepare_json_ext(self, instance):
            json_ext_data = instance.json_ext
            json_data = self.__flatten_dict(json_ext_data)
            return json_data

        def __flatten_dict(self, d, parent_key='', sep='__'):
            items = {}
            for k, v in d.items():
                new_key = f"{parent_key}{sep}{k}" if parent_key else k
                if isinstance(v, dict):
                    items.update(self.__flatten_dict(v, new_key, sep=sep))
                else:
                    items[new_key] = v
            return items

    @registry.register_document
    class GroupIndividualDocument(BaseSyncDocument):
        DASHBOARD_NAME = 'Group'

        group = opensearch_fields.ObjectField(properties={
            'id': opensearch_fields.KeywordField(),
            'code': opensearch_fields.KeywordField(),
            'json_ext': opensearch_fields.ObjectField(),
        })
        individual = opensearch_fields.ObjectField(properties={
            'first_name': opensearch_fields.KeywordField(),
            'last_name': opensearch_fields.KeywordField(),
            'dob': opensearch_fields.DateField(),
        })
        role = opensearch_fields.KeywordField()
        recipient_type = opensearch_fields.KeywordField(),
        json_ext = opensearch_fields.ObjectField()

        class Index:
            name = 'group_individual'
            settings = {
                'number_of_shards': 1,
                'number_of_replicas': 0
            }
            auto_refresh = auto_refresh

        class Django:
            model = GroupIndividual
            related_models = [Group, Individual]
            fields = [
                'id'
            ]
            queryset_pagination = 5000

        def get_instances_from_related(self, related_instance):
            if isinstance(related_instance, Group):
                return GroupIndividual.objects.filter(
                    group=related_instance
                )
            elif isinstance(related_instance, Individual):
                return GroupIndividual.objects.filter(individual=related_instance)

        def prepare_json_ext(self, instance):
            json_ext_data = instance.json_ext
            json_data = self.__flatten_dict(json_ext_data)
            return json_data

        def __flatten_dict(self, d, parent_key='', sep='__'):
            items = {}
            for k, v in d.items():
                new_key = f"{parent_key}{sep}{k}" if parent_key else k
                if isinstance(v, dict):
                    items.update(self.__flatten_dict(v, new_key, sep=sep))
                else:
                    items[new_key] = v
            return items

    @registry.register_document
    class IndividualDataSourceDocument(BaseSyncDocument):
        DASHBOARD_NAME = 'DataUpdates'

        source_name = opensearch_fields.KeywordField()
        source_type = opensearch_fields.KeywordField()
        date_created = opensearch_fields.DateField()
        status = opensearch_fields.KeywordField()
        error = opensearch_fields.ObjectField()

        class Index:
            name = 'individual_data_source_upload'
            settings = {
                'number_of_shards': 1,
                'number_of_replicas': 0
            }
            auto_refresh = auto_refresh

        class Django:
            model = IndividualDataSourceUpload
            fields = [
                'id'
            ]
            queryset_pagination = 5000