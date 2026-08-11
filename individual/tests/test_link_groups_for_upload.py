import uuid as uuidlib

from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import (
    Group,
    GroupIndividual,
    Individual,
    IndividualDataSource,
    IndividualDataSourceUpload,
    IndividualDataUploadRecords,
)
from individual.services import IndividualImportService


class LinkGroupsForUploadTest(TestCase):
    """
    Behaviour of link_groups_for_upload_uuid: household aggregation, role and
    recipient inference, PMT mirroring, and idempotency on re-run.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _upload_with(self, households, prefix="H"):
        """households: {code: [(role_code, hhrep, pmt_score|None), ...]}"""
        upload = IndividualDataSourceUpload(source_name="t.csv", source_type="csv")
        upload.save(username=self.user.username)
        IndividualDataUploadRecords(
            data_upload=upload,
            workflow="test",
            json_ext={"group_aggregation_column": "group_code"},
        ).save(user=self.user)

        created = {}
        for code, members in households.items():
            created[code] = []
            for idx, (role_code, hhrep, pmt) in enumerate(members):
                jx = {
                    "group_code": code,
                    "individual_role_code": role_code,
                    "hhrep": hhrep,
                }
                if pmt is not None:
                    jx["pmt_score"] = pmt
                    jx["pmt_class"] = "POOR"
                ind = Individual(
                    first_name=f"{prefix}{code}{idx}",
                    last_name=code,
                    dob="1990-01-01",
                    json_ext=jx,
                    user_created=self.user,
                    user_updated=self.user,
                    id=uuidlib.uuid4(),
                )
                ind.save(user=self.user)
                IndividualDataSource(
                    upload=upload,
                    individual=ind,
                    json_ext={},
                    validations={},
                    user_created=self.user,
                    user_updated=self.user,
                    id=uuidlib.uuid4(),
                ).save(user=self.user)
                created[code].append(ind)
        return upload, created

    def _run(self, upload):
        return IndividualImportService(self.user).link_groups_for_upload_uuid(
            str(upload.uuid)
        )

    # ------------------------------------------------------------------

    def test_creates_one_group_per_code_and_links_every_member(self):
        upload, created = self._upload_with({
            "G1": [("1", "1", 10.0), ("3", "0", None), ("3", "0", None)],
            "G2": [("1", "1", 20.0), ("3", "0", None)],
        })
        res = self._run(upload)

        self.assertTrue(res["success"])
        self.assertEqual(res["created_groups"], 2)
        self.assertEqual(res["created_links"], 5)
        self.assertEqual(res["groups_touched"], 2)
        self.assertEqual(Group.objects.filter(code__in=["G1", "G2"]).count(), 2)
        for code, members in created.items():
            grp = Group.objects.get(code=code, is_deleted=False)
            self.assertEqual(
                GroupIndividual.objects.filter(group=grp, is_deleted=False).count(),
                len(members),
            )

    def test_head_and_primary_recipient_inferred(self):
        upload, created = self._upload_with({"G3": [("1", "1", 5.0), ("3", "0", None)]})
        self._run(upload)

        grp = Group.objects.get(code="G3", is_deleted=False)
        head_link = GroupIndividual.objects.get(
            group=grp, individual=created["G3"][0], is_deleted=False
        )
        other_link = GroupIndividual.objects.get(
            group=grp, individual=created["G3"][1], is_deleted=False
        )
        self.assertEqual(head_link.role, GroupIndividual.Role.HEAD)
        self.assertEqual(head_link.recipient_type, GroupIndividual.RecipientType.PRIMARY)
        self.assertNotEqual(other_link.role, GroupIndividual.Role.HEAD)

    def test_pmt_mirrored_from_head_to_group(self):
        upload, _ = self._upload_with({"G4": [("1", "1", 42.5), ("3", "0", None)]})
        self._run(upload)

        grp = Group.objects.get(code="G4", is_deleted=False)
        self.assertEqual(grp.json_ext.get("pmt_score_household"), 42.5)
        self.assertEqual(grp.json_ext.get("pmt_class_household"), "POOR")

    def test_group_aggregates_rebuilt_after_run(self):
        upload, created = self._upload_with({"G5": [("1", "1", 1.0), ("3", "0", None)]})
        self._run(upload)

        grp = Group.objects.get(code="G5", is_deleted=False)
        self.assertEqual(grp.json_ext.get("head_id"), str(created["G5"][0].id))
        self.assertEqual(len(grp.json_ext.get("members", {})), 2)

    def test_existing_group_is_reused_not_duplicated(self):
        existing = Group(code="G6", json_ext={})
        existing.save(user=self.user)

        upload, _ = self._upload_with({"G6": [("1", "1", 3.0)]})
        res = self._run(upload)

        self.assertEqual(res["created_groups"], 0)
        self.assertEqual(Group.objects.filter(code="G6", is_deleted=False).count(), 1)

    def test_rerun_is_idempotent(self):
        upload, _ = self._upload_with({"G7": [("1", "1", 7.0), ("3", "0", None)]})
        first = self._run(upload)
        second = self._run(upload)

        self.assertEqual(first["created_links"], 2)
        self.assertEqual(second["created_links"], 0, "re-run must not duplicate links")
        self.assertEqual(second["created_groups"], 0)
        grp = Group.objects.get(code="G7", is_deleted=False)
        self.assertEqual(
            GroupIndividual.objects.filter(group=grp, is_deleted=False).count(), 2
        )

    def test_individual_without_group_code_is_skipped(self):
        upload = IndividualDataSourceUpload(source_name="t.csv", source_type="csv")
        upload.save(username=self.user.username)
        ind = Individual(
            first_name="No", last_name="Group", dob="1990-01-01",
            json_ext={}, user_created=self.user, user_updated=self.user,
            id=uuidlib.uuid4(),
        )
        ind.save(user=self.user)
        IndividualDataSource(
            upload=upload, individual=ind, json_ext={}, validations={},
            user_created=self.user, user_updated=self.user, id=uuidlib.uuid4(),
        ).save(user=self.user)

        res = self._run(upload)
        self.assertTrue(res["success"])
        self.assertEqual(res["created_links"], 0)
        self.assertEqual(res["created_groups"], 0)

    def test_members_of_same_household_share_one_group(self):
        upload, created = self._upload_with(
            {"G8": [("1", "1", 9.0)] + [("3", "0", None)] * 5}
        )
        res = self._run(upload)

        self.assertEqual(res["created_groups"], 1)
        self.assertEqual(res["created_links"], 6)
        grp = Group.objects.get(code="G8", is_deleted=False)
        self.assertEqual(
            GroupIndividual.objects.filter(group=grp, is_deleted=False).count(), 6
        )
        self.assertEqual(
            GroupIndividual.objects.filter(
                group=grp, role=GroupIndividual.Role.HEAD, is_deleted=False
            ).count(),
            1,
            "exactly one HEAD per household",
        )
