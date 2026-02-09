# individual/documents.py

from django.apps import apps
from django.conf import settings

is_unit_test_env = getattr(settings, "IS_UNIT_TEST_ENV", False)

# Only register OpenSearch documents if opensearch_reports is installed
if "opensearch_reports" in apps.app_configs:
    from opensearch_reports.service import BaseSyncDocument
    from django_opensearch_dsl import fields as opensearch_fields
    from django_opensearch_dsl.registries import registry
    from individual.models import (
        Individual,
        IndividualDataSourceUpload,
        GroupIndividual,
        Group,
    )

    # Skip auto-refresh on model update when running unit tests to avoid connection issues
    auto_refresh = not is_unit_test_env

    @registry.register_document
    class IndividualDocument(BaseSyncDocument):
        """
        Index for Individuals.

        Important:
        - Do NOT flatten json_ext, because adapter stores full survey payload in json_ext["raw"].
        - Index only a small, predictable subset of json_ext keys used for search/filters/dashboards.
        """
        DASHBOARD_NAME = "Individual"

        # Top-level searchable fields (fast filters / exact matches)
        first_name = opensearch_fields.KeywordField()
        last_name = opensearch_fields.KeywordField()
        dob = opensearch_fields.DateField()
        date_created = opensearch_fields.DateField()

        # json_ext indexed as an object (but we will only populate whitelisted keys)
        json_ext = opensearch_fields.ObjectField()

        class Index:
            name = "individual"
            settings = {
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
            auto_refresh = auto_refresh

        class Django:
            model = Individual
            fields = [
                "id",
            ]
            # Keep batches reasonable to avoid huge bulk payloads
            queryset_pagination = 1000

        # Only index these json_ext keys (adapter keeps everything else in json_ext["raw"])
        INDEXED_JSON_KEYS = [
            "external_id",
            "group_code",
            "individual_role",
            "individual_role_code",
            "hhrep",
            "pmt_score",
            "pmt_class",
            "ss_batch",
            # add more small top-level keys here
        ]

        def prepare_json_ext(self, instance):
            jx = instance.json_ext or {}
            if not isinstance(jx, dict):
                return {}

            # Never index raw payload; adapter already stores survey variables under json_ext["raw"]
            out = {k: jx.get(k) for k in self.INDEXED_JSON_KEYS}

            # Optional: If you set location_str in services.py, you may want it searchable too.
            # It is stored in json_ext by your IndividualService._update_json_ext().
            if "location_str" in jx:
                out["location_str"] = jx.get("location_str")

            return out

    @registry.register_document
    class GroupIndividualDocument(BaseSyncDocument):
        """
        Index for GroupIndividual relations.
        """
        DASHBOARD_NAME = "Group"

        group = opensearch_fields.ObjectField(
            properties={
                "id": opensearch_fields.KeywordField(),
                "code": opensearch_fields.KeywordField(),
                "json_ext": opensearch_fields.ObjectField(),
            }
        )
        individual = opensearch_fields.ObjectField(
            properties={
                "first_name": opensearch_fields.KeywordField(),
                "last_name": opensearch_fields.KeywordField(),
                "dob": opensearch_fields.DateField(),
            }
        )
        role = opensearch_fields.KeywordField()
        recipient_type = opensearch_fields.KeywordField()
        json_ext = opensearch_fields.ObjectField()

        class Index:
            name = "group_individual"
            settings = {
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
            auto_refresh = auto_refresh

        class Django:
            model = GroupIndividual
            related_models = [Group, Individual]
            fields = [
                "id",
            ]
            queryset_pagination = 1000

        def get_instances_from_related(self, related_instance):
            if isinstance(related_instance, Group):
                return GroupIndividual.objects.filter(group=related_instance)
            if isinstance(related_instance, Individual):
                return GroupIndividual.objects.filter(individual=related_instance)
            return None

        INDEXED_JSON_KEYS = [
            # ONLY keys you need for OS dashboards/search here

        ]

        def prepare_json_ext(self, instance):
            jx = instance.json_ext or {}
            if not isinstance(jx, dict):
                return {}
            if not self.INDEXED_JSON_KEYS:
                # If you don't need json_ext indexed for GroupIndividual, keep empty
                return {}
            return {k: jx.get(k) for k in self.INDEXED_JSON_KEYS}

    @registry.register_document
    class IndividualDataSourceDocument(BaseSyncDocument):
        """
        Index for import/upload tracking.
        """
        DASHBOARD_NAME = "DataUpdates"

        source_name = opensearch_fields.KeywordField()
        source_type = opensearch_fields.KeywordField()
        date_created = opensearch_fields.DateField()
        status = opensearch_fields.KeywordField()
        error = opensearch_fields.ObjectField()

        class Index:
            name = "individual_data_source_upload"
            settings = {
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
            auto_refresh = auto_refresh

        class Django:
            model = IndividualDataSourceUpload
            fields = [
                "id",
            ]
            queryset_pagination = 1000
