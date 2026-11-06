from unittest.mock import patch

from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import Group, GroupIndividual, Individual, IndividualDataSource
from individual.pct_enrolment import PctEnrolmentService
from tasks_management.models import Task

CANDIDATES = "individual.pct_enrolment.district_enrolment_candidates"


@patch("individual.pct_enrolment.pct_programme_exists", return_value=True)
class PctDistrictEnrolmentTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api(username="pctdistrict")

    def _household(self, code):
        group = Group(code=code, json_ext={"pmt_class_household": "POOR"})
        group.save(user=self.user)
        head = Individual(first_name="H", last_name=code, dob="1980-01-01", json_ext={})
        head.save(user=self.user)
        GroupIndividual(group=group, individual=head, role=GroupIndividual.Role.HEAD).save(user=self.user)
        return group, head

    def _groups(self, households):
        return Group.objects.filter(id__in=[g.id for g, _ in households])

    def _staged(self, task_json):
        return set(IndividualDataSource.objects.filter(upload_id=task_json["upload_id"])
                   .values_list("individual_id", flat=True))

    def test_one_task_with_frozen_heads_and_no_duplicate(self, _):
        households = [self._household(f"PCTD-{i}") for i in range(3)]
        service = PctEnrolmentService(self.user)
        with patch(CANDIDATES, return_value=self._groups(households)):
            dry = service.raise_for_district("999", dry_run=True)
            first = service.raise_for_district("999")
            second = service.raise_for_district("999")

        self.assertEqual(dry, {"success": True, "households": 3, "dry_run": True})
        task = first["data"]
        self.assertEqual(task["json_ext"]["district_code"], "999")
        self.assertEqual(task["json_ext"]["households"], 3)
        self.assertEqual(self._staged(task["json_ext"]), {head.id for _, head in households})
        self.assertTrue(second["existing"])
        self.assertEqual(second["task_id"], str(task["id"]))

    def test_new_households_join_the_open_task(self, _):
        early = [self._household(f"PCTE-{i}") for i in range(2)]
        late = [self._household("PCTE-late")]
        service = PctEnrolmentService(self.user)
        with patch(CANDIDATES, return_value=self._groups(early)):
            first = service.raise_for_district("997")
        with patch(CANDIDATES, return_value=self._groups(early + late)):
            second = service.raise_for_district("997")

        task = Task.objects.get(id=first["data"]["id"])
        self.assertEqual(second["added"], 1)
        self.assertEqual(task.json_ext["households"], 3)
        self.assertEqual(self._staged(task.json_ext), {head.id for _, head in early + late})

    def test_review_started_gives_a_new_task_without_overlap(self, _):
        early = [self._household("PCTR-1")]
        late = [self._household("PCTR-2")]
        service = PctEnrolmentService(self.user)
        with patch(CANDIDATES, return_value=self._groups(early)):
            first = service.raise_for_district("996")
        Task.objects.filter(id=first["data"]["id"]).update(status=Task.Status.ACCEPTED)
        with patch(CANDIDATES, return_value=self._groups(early + late)):
            second = service.raise_for_district("996")

        self.assertNotEqual(second["data"]["id"], first["data"]["id"])
        self.assertEqual(self._staged(second["data"]["json_ext"]), {late[0][1].id})

    def test_nothing_to_enrol_raises_no_task(self, _):
        with patch(CANDIDATES, return_value=Group.objects.none()):
            result = PctEnrolmentService(self.user).raise_for_district("998")
        self.assertEqual(result, {"success": True, "households": 0, "dry_run": False})

    @patch("individual.pct_enrolment.district_codes", return_value=["995", "994"])
    def test_upload_routes_to_its_districts(self, _codes, _):
        service = PctEnrolmentService(self.user)
        candidates = self._groups([self._household("PCTU-1")])
        with patch("individual.pct_enrolment.upload_enrolment_candidates", return_value=candidates), \
                patch.object(service, "raise_for_district", side_effect=lambda code: {"code": code}) as raised:
            result = service.raise_for_upload("upload-id")
        self.assertEqual(result, {"995": {"code": "995"}, "994": {"code": "994"}})
        self.assertEqual(raised.call_count, 2)
