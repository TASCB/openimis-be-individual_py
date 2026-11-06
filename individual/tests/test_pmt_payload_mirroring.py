from django.test import SimpleTestCase

from individual.models import Group, GroupIndividual, Individual
from individual.services import GroupAndGroupIndividualAlignmentService, IndividualImportService


class PmtPayloadMirroringTest(SimpleTestCase):
    def test_nested_import_results_survive_repeated_group_rebuilds(self):
        for classification in ("POOR", "NON_POOR"):
            with self.subTest(classification=classification):
                head = Individual(first_name="Test", last_name="Head", json_ext={
                    "json_ext": {"pmt_score": 0, "pmt_class": classification, "pmt_cutoff_used": 11.01},
                })
                group = Group(code="PMT-test", json_ext={"unrelated": "value"})
                link = GroupIndividual(individual=head, group=group, role=GroupIndividual.Role.HEAD)
                service = GroupAndGroupIndividualAlignmentService(None)
                for _ in range(2):
                    service.update_json_ext_for_group(group, links=[link], save=False)
                    self.assertEqual(group.json_ext["pmt_class_household"], classification)
                    self.assertEqual(group.json_ext["pmt_score_household"], 0)
                    self.assertEqual(group.json_ext["pmt_cutoff_used"], 11.01)

    def test_explicit_top_level_result_takes_precedence(self):
        source = {"pmt_class": "POOR", "json_ext": {"pmt_class": "NON_POOR"}}
        self.assertEqual(IndividualImportService._json_ext_lookup(source, "pmt_class"), "POOR")

    def test_empty_top_level_result_falls_back_to_import_payload(self):
        source = {"pmt_score": None, "json_ext": {"pmt_score": 0}}
        self.assertEqual(IndividualImportService._json_ext_lookup(source, "pmt_score"), 0)
