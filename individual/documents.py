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

    def extract_gender(json_ext):
        """Pull a normalized gender ("M"/"F") out of an Individual.json_ext.

        Tolerates the flat shape and the legacy nested ``json_ext['json_ext']``
        shape, and the value being stored under either ``gender`` or ``sex``.
        """
        jx = json_ext or {}
        if not isinstance(jx, dict):
            return None
        nested = jx.get('json_ext') if isinstance(jx.get('json_ext'), dict) else {}
        value = (
            jx.get('gender') or jx.get('sex')
            or nested.get('gender') or nested.get('sex')
        )
        if value in (None, ''):
            return None
        value = str(value).strip().upper()
        return value or None

    @registry.register_document
    class IndividualDocument(BaseSyncDocument):
        DASHBOARD_NAME = 'Individual'

        first_name = opensearch_fields.KeywordField()
        last_name = opensearch_fields.KeywordField()
        dob = opensearch_fields.DateField()
        gender = opensearch_fields.KeywordField()
        date_created = opensearch_fields.DateField()
        json_ext = opensearch_fields.ObjectField(dynamic=False)

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

        def prepare_gender(self, instance):
            return extract_gender(getattr(instance, 'json_ext', None))

        def prepare_json_ext(self, instance):
            return {}

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
            'location_code': opensearch_fields.KeywordField(),
            'location_name': opensearch_fields.KeywordField(),
            'head': opensearch_fields.KeywordField(),
            'head_id': opensearch_fields.KeywordField(),
            'primary_recipient': opensearch_fields.KeywordField(),
            'primary_recipient_id': opensearch_fields.KeywordField(),
            'secondary_recipient': opensearch_fields.KeywordField(),
            'secondary_recipient_id': opensearch_fields.KeywordField(),
            'pmt_score_household': opensearch_fields.FloatField(),
            'pmt_class_household': opensearch_fields.KeywordField(),
            'consent_res': opensearch_fields.KeywordField(),
            'pssn_wave': opensearch_fields.KeywordField(),
            'member_count': opensearch_fields.IntegerField(),
        })
        individual = opensearch_fields.ObjectField(properties={
            'first_name': opensearch_fields.KeywordField(),
            'last_name': opensearch_fields.KeywordField(),
            'dob': opensearch_fields.DateField(),
            'gender': opensearch_fields.KeywordField(),
        })
        role = opensearch_fields.KeywordField()
        recipient_type = opensearch_fields.KeywordField()
        json_ext = opensearch_fields.ObjectField(dynamic=False)

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

        def prepare_individual(self, instance):
            ind = instance.individual
            return {
                'first_name': ind.first_name,
                'last_name': ind.last_name,
                'dob': ind.dob,
                'gender': extract_gender(getattr(ind, 'json_ext', None)),
            }

        def prepare_group(self, instance):
            group = instance.group
            json_ext = group.json_ext or {}
            members = json_ext.get('members') or {}
            pmt_score = json_ext.get('pmt_score_household')
            try:
                pmt_score = float(pmt_score) if pmt_score not in (None, '') else None
            except (TypeError, ValueError):
                pmt_score = None
            return {
                'id': str(group.id),
                'code': group.code,
                'location_code': json_ext.get('location_code'),
                'location_name': json_ext.get('location_name'),
                'head': json_ext.get('head'),
                'head_id': json_ext.get('head_id'),
                'primary_recipient': json_ext.get('primary_recipient'),
                'primary_recipient_id': json_ext.get('primary_recipient_id'),
                'secondary_recipient': json_ext.get('secondary_recipient'),
                'secondary_recipient_id': json_ext.get('secondary_recipient_id'),
                'pmt_score_household': pmt_score,
                'pmt_class_household': json_ext.get('pmt_class_household'),
                'consent_res': json_ext.get('consent_res'),
                'pssn_wave': json_ext.get('pssn_wave'),
                'member_count': len(members) if isinstance(members, dict) else None,
            }

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
