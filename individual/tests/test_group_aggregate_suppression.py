from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import (
    Group,
    GroupIndividual,
    group_aggregates_suppressed,
    suppress_group_aggregate_updates,
)
from individual.services import GroupAndGroupIndividualAlignmentService
from individual.tests.test_helpers import create_individual, create_group


def _group_versions(group_id):
    """How many times the group row has been written (history rows)."""
    return Group.history.filter(id=group_id).count()


class GroupAggregateSuppressionTest(TestCase):
    """
    Covers the opt-out used by the bulk import path to avoid rewriting a group
    row once per member link.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()
        cls.username = cls.user.username

    def _link(self, group, individual, role=None):
        gi = GroupIndividual(group=group, individual=individual, role=role)
        gi.save(user=self.user)
        return gi

    # ------------------------- flag mechanics -------------------------

    def test_flag_defaults_to_off(self):
        self.assertFalse(group_aggregates_suppressed())

    def test_flag_set_inside_and_restored_after(self):
        with suppress_group_aggregate_updates():
            self.assertTrue(group_aggregates_suppressed())
        self.assertFalse(group_aggregates_suppressed())

    def test_flag_restored_after_exception(self):
        with self.assertRaises(ValueError):
            with suppress_group_aggregate_updates():
                raise ValueError("boom")
        self.assertFalse(group_aggregates_suppressed())

    def test_nesting_restores_outer_state(self):
        with suppress_group_aggregate_updates():
            with suppress_group_aggregate_updates():
                self.assertTrue(group_aggregates_suppressed())
            self.assertTrue(group_aggregates_suppressed())
        self.assertFalse(group_aggregates_suppressed())

    # ------------------------- default behaviour is unchanged -------------------------

    def test_without_suppression_group_is_rebuilt_on_each_link(self):
        group = create_group(self.username)
        before = _group_versions(group.id)

        head = create_individual(self.username)
        self._link(group, head, GroupIndividual.Role.HEAD)
        member = create_individual(self.username)
        self._link(group, member)

        self.assertGreater(
            _group_versions(group.id) - before,
            0,
            "interactive saves must keep rebuilding the group aggregates",
        )
        group.refresh_from_db()
        self.assertEqual(group.json_ext.get("head_id"), str(head.id))
        self.assertIn(str(member.id), group.json_ext.get("members", {}))

    # ------------------------- suppression -------------------------

    def test_suppression_stops_the_per_save_group_rewrite(self):
        group = create_group(self.username)
        before = _group_versions(group.id)

        with suppress_group_aggregate_updates():
            head = create_individual(self.username)
            self._link(group, head, GroupIndividual.Role.HEAD)
            for _ in range(3):
                self._link(group, create_individual(self.username))

        self.assertEqual(
            _group_versions(group.id),
            before,
            "no group row should be written while suppressed",
        )

    def test_forced_rebuild_after_suppression_produces_correct_aggregates(self):
        group = create_group(self.username)

        with suppress_group_aggregate_updates():
            head = create_individual(self.username)
            self._link(group, head, GroupIndividual.Role.HEAD)
            members = [create_individual(self.username) for _ in range(3)]
            for m in members:
                self._link(group, m)

        versions_while_suppressed = _group_versions(group.id)

        GroupAndGroupIndividualAlignmentService(self.user).update_json_ext_for_group(
            group, force=True
        )

        self.assertEqual(
            _group_versions(group.id) - versions_while_suppressed,
            1,
            "the end-of-run rebuild should write the group exactly once",
        )

        group.refresh_from_db()
        self.assertEqual(group.json_ext.get("head_id"), str(head.id))
        for m in members:
            self.assertIn(str(m.id), group.json_ext.get("members", {}))

    def test_suppression_writes_fewer_group_rows_than_default(self):
        """The point of the change, stated as an assertion."""
        plain = create_group(self.username)
        plain_before = _group_versions(plain.id)
        self._link(plain, create_individual(self.username), GroupIndividual.Role.HEAD)
        for _ in range(3):
            self._link(plain, create_individual(self.username))
        plain_writes = _group_versions(plain.id) - plain_before

        suppressed = create_group(self.username)
        suppressed_before = _group_versions(suppressed.id)
        with suppress_group_aggregate_updates():
            self._link(
                suppressed, create_individual(self.username), GroupIndividual.Role.HEAD
            )
            for _ in range(3):
                self._link(suppressed, create_individual(self.username))
        GroupAndGroupIndividualAlignmentService(self.user).update_json_ext_for_group(
            suppressed, force=True
        )
        suppressed_writes = _group_versions(suppressed.id) - suppressed_before

        self.assertLess(suppressed_writes, plain_writes)
        self.assertEqual(suppressed_writes, 1)

    def test_force_ignores_the_flag(self):
        group = create_group(self.username)
        with suppress_group_aggregate_updates():
            self._link(group, create_individual(self.username), GroupIndividual.Role.HEAD)
            before = _group_versions(group.id)
            GroupAndGroupIndividualAlignmentService(
                self.user
            ).update_json_ext_for_group(group, force=True)
            self.assertEqual(_group_versions(group.id) - before, 1)
