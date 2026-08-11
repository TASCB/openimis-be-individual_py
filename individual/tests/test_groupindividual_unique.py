import uuid as uuidlib

from django.db import IntegrityError, transaction
from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import (
    Group,
    GroupIndividual,
    Individual,
    suppress_group_aggregate_updates,
)


class GroupIndividualActiveUniqueTest(TestCase):
    """Partial unique index on the active (group, individual) pair."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _pair(self, code):
        grp = Group(code=code, json_ext={})
        grp.save(user=self.user)
        ind = Individual(
            first_name="A", last_name="B", dob="1990-01-01", json_ext={},
            user_created=self.user, user_updated=self.user, id=uuidlib.uuid4(),
        )
        ind.save(user=self.user)
        return grp, ind

    def test_duplicate_active_link_is_rejected(self):
        grp, ind = self._pair("UQ1")
        with suppress_group_aggregate_updates():
            GroupIndividual(
                group=grp, individual=ind, role=GroupIndividual.Role.HEAD
            ).save(user=self.user)

            with self.assertRaises(IntegrityError):
                with transaction.atomic():
                    GroupIndividual(group=grp, individual=ind).save(user=self.user)

    def test_relink_allowed_after_soft_delete(self):
        grp, ind = self._pair("UQ2")
        with suppress_group_aggregate_updates():
            gi = GroupIndividual(group=grp, individual=ind)
            gi.save(user=self.user)
            gi.is_deleted = True
            gi.save(user=self.user)

            GroupIndividual(group=grp, individual=ind).save(user=self.user)

        self.assertEqual(
            GroupIndividual.objects.filter(
                group=grp, individual=ind, is_deleted=False
            ).count(),
            1,
        )

    def test_same_individual_may_belong_to_another_group(self):
        grp1, ind = self._pair("UQ3")
        grp2 = Group(code="UQ4", json_ext={})
        grp2.save(user=self.user)
        with suppress_group_aggregate_updates():
            GroupIndividual(group=grp1, individual=ind).save(user=self.user)
            GroupIndividual(group=grp2, individual=ind).save(user=self.user)

        self.assertEqual(
            GroupIndividual.objects.filter(individual=ind, is_deleted=False).count(), 2
        )
