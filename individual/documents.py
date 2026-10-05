from django.apps import apps
from django.conf import settings

is_unit_test_env = getattr(settings, 'IS_UNIT_TEST_ENV', False)

# Check if the 'opensearch_reports' app is in INSTALLED_APPS
if 'opensearch_reports' in apps.app_configs:
    from datetime import date
    from functools import lru_cache

    from location.models import Location
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

    def extract_disability(json_ext):
        jx = json_ext if isinstance(json_ext, dict) else {}
        nested = jx.get('json_ext') if isinstance(jx.get('json_ext'), dict) else {}
        value = jx.get('disability', nested.get('disability'))
        if value in (None, ''):
            return None
        return 'YES' if str(value).strip().upper() in ('1', 'TRUE', 'YES', 'Y') else 'NO'

    @lru_cache(maxsize=65536)
    def location_names(location_id):
        """Location type -> name for the location and its ancestors. Callers must not mutate it."""
        names = {}
        while location_id:
            row = Location.objects.filter(id=location_id).values('type', 'name', 'parent_id').first()
            if not row:
                break
            names.setdefault(row['type'], row['name'])
            location_id = row['parent_id']
        return names

    def region_and_district(location_id):
        names = location_names(location_id)
        return names.get('R'), names.get('D')

    def household_pmt_class(individual):
        membership = (GroupIndividual.objects
                      .filter(individual=individual, is_deleted=False, is_active=True)
                      .order_by('-date_created').values('group__json_ext').first())
        group_ext = (membership or {}).get('group__json_ext') or {}
        return group_ext.get('pmt_class_household') or (individual.json_ext or {}).get('pmt_class')

    @registry.register_document
    class IndividualDocument(BaseSyncDocument):
        DASHBOARD_NAME = 'Individual'

        first_name = opensearch_fields.KeywordField()
        last_name = opensearch_fields.KeywordField()
        dob = opensearch_fields.DateField()
        gender = opensearch_fields.KeywordField()
        date_created = opensearch_fields.DateField()
        region = opensearch_fields.KeywordField()
        district = opensearch_fields.KeywordField()
        disability = opensearch_fields.KeywordField()
        pmt_class = opensearch_fields.KeywordField()
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

        def prepare_region(self, instance):
            return region_and_district(instance.location_id)[0]

        def prepare_district(self, instance):
            return region_and_district(instance.location_id)[1]

        def prepare_disability(self, instance):
            return extract_disability(instance.json_ext)

        def prepare_pmt_class(self, instance):
            return household_pmt_class(instance)

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
            'region': opensearch_fields.KeywordField(),
            'district': opensearch_fields.KeywordField(),
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
            'disability': opensearch_fields.KeywordField(),
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
                'disability': extract_disability(getattr(ind, 'json_ext', None)),
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
            region, district = region_and_district(group.location_id)
            return {
                'id': str(group.id),
                'code': group.code,
                'location_code': json_ext.get('location_code'),
                'location_name': json_ext.get('location_name'),
                'region': region,
                'district': district,
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

    if 'social_protection' in apps.app_configs:
        from social_protection.models import GroupBeneficiary

        def age_on(dob, today):
            if not dob:
                return None
            return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

        def any_yes(values):
            values = [v for v in values if v]
            return 'YES' if 'YES' in values else ('NO' if values else None)

        @registry.register_document
        class GroupBeneficiaryDocument(BaseSyncDocument):
            """One enrolled household per document. Member ages are as of the indexing date."""
            DASHBOARD_NAME = 'Beneficiary'

            benefit_plan = opensearch_fields.ObjectField(properties={
                'code': opensearch_fields.KeywordField(),
                'name': opensearch_fields.KeywordField(),
            })
            status = opensearch_fields.KeywordField()
            date_enrolled = opensearch_fields.DateField()
            region = opensearch_fields.KeywordField()
            district = opensearch_fields.KeywordField()
            ward = opensearch_fields.KeywordField()
            village = opensearch_fields.KeywordField()
            group = opensearch_fields.ObjectField(properties={
                'id': opensearch_fields.KeywordField(),
                'code': opensearch_fields.KeywordField(),
                'member_count': opensearch_fields.IntegerField(),
                'pmt_class': opensearch_fields.KeywordField(),
                'pmt_score': opensearch_fields.FloatField(),
                'pssn_wave': opensearch_fields.KeywordField(),
                'consent_res': opensearch_fields.KeywordField(),
            })
            head = opensearch_fields.ObjectField(properties={
                'gender': opensearch_fields.KeywordField(),
                'dob': opensearch_fields.DateField(),
                'disability': opensearch_fields.KeywordField(),
            })
            any_member_disability = opensearch_fields.KeywordField()
            children_under_5 = opensearch_fields.IntegerField()
            elderly_60_plus = opensearch_fields.IntegerField()

            class Index:
                name = 'group_beneficiary'
                settings = {
                    'number_of_shards': 1,
                    'number_of_replicas': 0
                }
                auto_refresh = auto_refresh

            class Django:
                model = GroupBeneficiary
                related_models = [Group, GroupIndividual]
                fields = [
                    'id'
                ]
                queryset_pagination = 5000

            def get_instances_from_related(self, related_instance):
                if isinstance(related_instance, Group):
                    return GroupBeneficiary.objects.filter(group=related_instance)
                if isinstance(related_instance, GroupIndividual):
                    return GroupBeneficiary.objects.filter(group_id=related_instance.group_id)

            def update(self, thing, action, *args, **kwargs):
                if action == 'index' and getattr(thing, 'is_deleted', False):
                    action = 'delete'
                return super().update(thing, action, *args, **kwargs)

            def prepare(self, instance):
                group = instance.group
                ext = group.json_ext or {}
                try:
                    pmt_score = float(ext.get('pmt_score_household'))
                except (TypeError, ValueError):
                    pmt_score = None
                members = list(GroupIndividual.objects
                               .filter(group_id=group.id, is_deleted=False, is_active=True)
                               .values('role', 'individual__dob', 'individual__json_ext'))
                head = next((m for m in members if m['role'] == GroupIndividual.Role.HEAD), None)
                today = date.today()
                ages = [age_on(m['individual__dob'], today) for m in members]
                places = location_names(group.location_id)
                return {
                    'id': str(instance.id),
                    'benefit_plan': {'code': instance.benefit_plan.code, 'name': instance.benefit_plan.name},
                    'status': instance.status,
                    'date_enrolled': instance.date_created,
                    'region': places.get('R'),
                    'district': places.get('D'),
                    'ward': places.get('W'),
                    'village': places.get('V'),
                    'group': {
                        'id': str(group.id),
                        'code': group.code,
                        'member_count': len(members),
                        'pmt_class': ext.get('pmt_class_household'),
                        'pmt_score': pmt_score,
                        'pssn_wave': ext.get('pssn_wave'),
                        'consent_res': ext.get('consent_res'),
                    },
                    'head': {
                        'gender': extract_gender(head['individual__json_ext']) if head else None,
                        'dob': head['individual__dob'] if head else None,
                        'disability': extract_disability(head['individual__json_ext']) if head else None,
                    },
                    'any_member_disability': any_yes(extract_disability(m['individual__json_ext']) for m in members),
                    'children_under_5': sum(1 for a in ages if a is not None and a < 5),
                    'elderly_60_plus': sum(1 for a in ages if a is not None and a >= 60),
                }
