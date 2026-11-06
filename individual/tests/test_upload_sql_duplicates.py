import uuid

from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import Individual, IndividualDataSource, IndividualDataSourceUpload
from individual.workflows.individual_upload_valid import upload_sql, upload_sql_partial
from individual.workflows.utils import SqlProcedurePythonWorkflow


class UploadSqlRepeatedExternalIdTest(TestCase):
    # A and A' are one person repeated; E and F share a household-level id but are different people.
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api(username="uploadsqldup")

    def _upload(self, tag):
        upload = IndividualDataSourceUpload(source_name=f"{tag}.csv", source_type="individual import")
        upload.save(user=self.user)
        rows = [("A", f" {tag}-1 "), ("A", f"{tag.lower()}-1"), ("B", f"{tag}-2"), ("C", None), ("D", None),
                ("E", f"{tag}-HH"), ("F", f"{tag}-HH")]
        sources = []
        for name, external_id in rows:
            payload = {"first_name": name, "last_name": tag, "dob": "1990-01-01", "location_code": None}
            if external_id is not None:
                payload["external_id"] = external_id
            source = IndividualDataSource(upload=upload, json_ext=payload, validations={},
                                          user_created=self.user, user_updated=self.user, uuid=uuid.uuid4())
            source.save(user=self.user)
            sources.append(source)
        return upload, sources

    def _check(self, upload, tag):
        people = Individual.objects.filter(last_name=tag, is_deleted=False)
        self.assertEqual(sorted(p.first_name for p in people), ["A", "B", "C", "D", "E", "F"])
        linked = list(IndividualDataSource.objects.filter(upload=upload).values_list("individual_id", flat=True))
        self.assertNotIn(None, linked)
        self.assertEqual(len(set(linked)), 6)

    def test_full_upload_inserts_a_repeated_person_once(self):
        upload, _ = self._upload("DUPF")
        SqlProcedurePythonWorkflow(upload.id, self.user.id).execute(upload_sql, [str(upload.id), str(self.user.id)])
        self._check(upload, "DUPF")

    def test_partial_approval_inserts_a_repeated_person_once(self):
        upload, sources = self._upload("DUPP")
        accepted = [str(s.id) for s in sources]
        SqlProcedurePythonWorkflow(upload.id, self.user.id, accepted).execute(
            upload_sql_partial, [str(upload.id), str(self.user.id), accepted])
        self._check(upload, "DUPP")
