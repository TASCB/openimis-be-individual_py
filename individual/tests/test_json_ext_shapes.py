from unittest import TestCase

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase as DjangoTestCase

from core.test_helpers import LogInHelper
from individual.workflows.utils import SqlProcedurePythonWorkflow
from workflow.exceptions import PythonWorkflowHandlerException

from individual.gql_queries import _json_ext_payload, _json_ext_raw
from individual.services import IndividualImportService


class FlattenJsonExtColumnTest(TestCase):
    flatten = staticmethod(IndividualImportService._flatten_json_ext_column)

    def test_string_column_is_merged_to_top_level(self):
        row = {"first_name": "A", "consent_res": None,
               "json_ext": '{"consent_res": "1", "pmt_score": 10.5, "raw": {"hh_size": "4"}}'}
        self.assertEqual(self.flatten(row), {
            "first_name": "A", "consent_res": "1", "pmt_score": 10.5, "raw": {"hh_size": "4"},
        })

    def test_non_empty_column_wins_over_json_ext(self):
        row = {"gender": "M", "json_ext": {"gender": "F", "pssn_wave": "3"}}
        self.assertEqual(self.flatten(row), {"gender": "M", "pssn_wave": "3"})

    def test_empty_or_broken_json_ext_adds_nothing(self):
        for value in (None, "", "nan", "not json", "[1, 2]"):
            self.assertEqual(self.flatten({"first_name": "A", "json_ext": value}), {"first_name": "A"})


class JsonExtLookupTest(TestCase):
    lookup = staticmethod(IndividualImportService._json_ext_lookup)

    def test_reads_top_level_raw(self):
        self.assertEqual(self.lookup({"raw": {"rel_to_hhh": "1"}}, "rel_to_hhh"), "1")

    def test_reads_nested_raw(self):
        self.assertEqual(self.lookup({"json_ext": {"raw": {"rel_to_hhh": "2"}}}, "rel_to_hhh"), "2")


class JsonExtPayloadTest(TestCase):
    def test_top_level_wins_over_nested(self):
        ext = {"pmt_score": 9.1, "json_ext": {"pmt_score": 11.6, "consent_res": "1", "raw": {"a": 1}}}
        payload = _json_ext_payload(ext)
        self.assertEqual(payload["pmt_score"], 9.1)
        self.assertEqual(payload["consent_res"], "1")
        self.assertEqual(_json_ext_raw(ext), {"a": 1})

    def test_flat_shape_unchanged(self):
        ext = {"consent_res": "2", "raw": {"b": 2}}
        self.assertIs(_json_ext_payload(ext), ext)
        self.assertEqual(_json_ext_raw(ext), {"b": 2})


class PayloadFieldsPassHeaderCheckTest(DjangoTestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = LogInHelper().get_or_create_user_api(username="payloadfields")

    def _upload(self, header, row):
        csv_file = SimpleUploadedFile("payload.csv", f"{header}\n{row}\n".encode(), content_type="text/csv")
        return IndividualImportService(self.user)._save_sources(csv_file)

    def test_json_ext_keys_are_accepted_but_unknown_columns_are_not(self):
        upload = self._upload(
            "first_name,last_name,dob,location_name,location_code,json_ext",
            'A,B,1980-01-01,X,1,"{""raw"": {""hh_size"": ""4""}, ""pmt_cutoff_used"": 11.01}"',
        )
        self.assertEqual(upload.json_ext["payload_fields"], ["pmt_cutoff_used", "raw"])
        SqlProcedurePythonWorkflow(upload.id, self.user.id).validate_dataframe_headers()

        bad = self._upload("first_name,last_name,dob,location_name,location_code,typo_column", "A,B,1980-01-01,X,1,x")
        with self.assertRaises(PythonWorkflowHandlerException):
            SqlProcedurePythonWorkflow(bad.id, self.user.id).validate_dataframe_headers()
