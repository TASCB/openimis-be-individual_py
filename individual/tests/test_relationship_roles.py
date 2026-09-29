import uuid as uuidlib
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from core.test_helpers import LogInHelper
from individual.models import Group, GroupIndividual, Individual
from individual.relationship_roles import (
    CODEBOOK,
    GENDER_BY_ROLE,
    describe_relationship,
    label_for_relationship,
    role_for_relationship,
)


class RelationshipRoleTableTest(SimpleTestCase):
    """
    The code table itself. Codes 5, 6 and 7 were each one slot off and 8 was
    unmapped, which stored grandchildren as siblings, the head's parents as
    grandchildren, and grandparents as parents.
    """

    def test_ungendered_codes(self):
        self.assertEqual(role_for_relationship("1", None), "HEAD")
        self.assertEqual(role_for_relationship("2", None), "SPOUSE")

    def test_children_are_code_3(self):
        self.assertEqual(role_for_relationship("3", "M"), "SON")
        self.assertEqual(role_for_relationship("3", "F"), "DAUGHTER")

    def test_code_5_is_a_grandchild_not_a_sibling(self):
        self.assertEqual(role_for_relationship("5", "M"), "GRANDSON")
        self.assertEqual(role_for_relationship("5", "F"), "GRANDDAUGHTER")

    def test_code_6_is_a_parent_not_a_grandchild(self):
        self.assertEqual(role_for_relationship("6", "M"), "FATHER")
        self.assertEqual(role_for_relationship("6", "F"), "MOTHER")
        self.assertEqual(label_for_relationship("6", "M"), "BIOLOGICAL FATHER")

    def test_code_8_is_the_sibling_code(self):
        self.assertEqual(role_for_relationship("8", "M"), "BROTHER")
        self.assertEqual(role_for_relationship("8", "F"), "SISTER")

    def test_in_laws_get_their_own_roles(self):
        """Codes 4, 7 and 12 are in-laws and no longer collapse together."""
        for code, male, female in (
            ("4", "SON IN LAW", "DAUGHTER IN LAW"),
            ("7", "FATHER IN LAW", "MOTHER IN LAW"),
            ("12", "BROTHER IN LAW", "SISTER IN LAW"),
        ):
            with self.subTest(code=code):
                self.assertEqual(describe_relationship(code, "M"), (male, male))
                self.assertEqual(describe_relationship(code, "F"), (female, female))

    def test_code_7_is_a_parent_in_law_not_a_grandparent(self):
        self.assertEqual(label_for_relationship("7", "M"), "FATHER IN LAW")
        self.assertNotEqual(role_for_relationship("7", "M"), "GRANDFATHER")

    def test_every_code_has_a_distinct_role(self):
        """
        Nothing collapses any more: a report can tell each relationship apart
        without parsing raw codes. Only code 9 is genuinely OTHER RELATIVE.
        """
        collapsed = [
            code
            for code in CODEBOOK
            if code != "9" and role_for_relationship(code, "F") == "OTHER RELATIVE"
        ]
        self.assertEqual(collapsed, [])

    def test_grandparent_roles_are_unreachable_from_the_questionnaire(self):
        """No code yields GRANDFATHER/GRANDMOTHER; they exist only in the enum."""
        produced = set()
        for code in list(CODEBOOK) + ["99"]:
            for gender in ("M", "F", None):
                produced.add(role_for_relationship(code, gender))
        self.assertNotIn("GRANDFATHER", produced)
        self.assertNotIn("GRANDMOTHER", produced)

    def test_co_wife_is_female_and_a_male_contradicts_the_code(self):
        """
        Only a woman holds code 11, so an unrecorded sex still resolves, but a
        male contradicts the code and widens rather than asserting it.
        """
        self.assertEqual(describe_relationship("11", "F"), ("CO-WIFE", "CO-WIFE"))
        self.assertEqual(describe_relationship("11", None), ("CO-WIFE", "CO-WIFE"))
        self.assertEqual(role_for_relationship("11", "M"), "OTHER RELATIVE")
        self.assertEqual(GENDER_BY_ROLE["CO-WIFE"], "F")

    def test_house_help_keeps_the_swahili_label(self):
        self.assertEqual(
            describe_relationship("13", None), ("MSAIDIZI WA NYUMBANI", "HOUSE HELP")
        )

    def test_step_child_is_its_own_role(self):
        self.assertEqual(describe_relationship("10", "M"), ("STEP CHILD", "STEP CHILD"))

    def test_unknown_sex_on_a_gendered_code_degrades_not_guesses(self):
        for code in ("3", "4", "5", "6", "7", "8", "12"):
            with self.subTest(code=code):
                self.assertEqual(role_for_relationship(code, None), "OTHER RELATIVE")

    def test_unknown_sex_on_an_ungendered_code_still_resolves(self):
        self.assertEqual(role_for_relationship("10", None), "STEP CHILD")
        self.assertEqual(role_for_relationship("13", None), "HOUSE HELP")

    def test_sex_aliases(self):
        self.assertEqual(role_for_relationship("5", "male"), "GRANDSON")
        self.assertEqual(role_for_relationship("5", "FEMALE"), "GRANDDAUGHTER")

    def test_code_outside_the_codebook_is_not_defined(self):
        self.assertEqual(describe_relationship("99", "M"), ("NOT DEFINED", "OTHER RELATIVE"))
        self.assertEqual(describe_relationship("", "M"), (None, None))
        self.assertEqual(describe_relationship(None, "M"), (None, None))

    def test_every_mapped_role_already_exists_in_the_enum(self):
        """
        Every role the codebook emits must exist in GroupIndividual.Role.
        A code added here without its enum value would fail only at write time.
        """
        valid = {role.value for role in GroupIndividual.Role}
        for code, entry in CODEBOOK.items():
            with self.subTest(code=code):
                self.assertIn(entry.male.role, valid)
                self.assertIn(entry.female.role, valid)
                self.assertIn(entry.unknown.role, valid)

    def test_only_genuinely_gendered_roles_reverse_to_a_sex(self):
        self.assertEqual(GENDER_BY_ROLE["GRANDSON"], "M")
        self.assertEqual(GENDER_BY_ROLE["SISTER"], "F")
        # OTHER RELATIVE is produced for both sexes, so it must not imply one.
        self.assertNotIn("OTHER RELATIVE", GENDER_BY_ROLE)
        self.assertEqual(len(GENDER_BY_ROLE), 15)


class BackfillGroupIndividualRolesTest(TestCase):
    """
    The repair command. Members are built carrying the roles the old table
    produced, with the source code recoverable only from external_id, which is
    the situation on the imported data.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api()

    def _member(self, group, code, role, ordinal, stored_label=None, gender=None):
        json_ext = {"external_id": f"P3-070405101-00123456-{code}-{ordinal:02d}"}
        if stored_label:
            json_ext["individual_role"] = stored_label
        if gender:
            json_ext["gender"] = gender
        individual = Individual(
            id=uuidlib.uuid4(),
            first_name=f"M{ordinal}",
            last_name="Test",
            dob="1950-01-01",
            json_ext=json_ext,
            user_created=self.user,
            user_updated=self.user,
        )
        individual.save(user=self.user)
        link = GroupIndividual(
            id=uuidlib.uuid4(),
            group=group,
            individual=individual,
            role=role,
            user_created=self.user,
            user_updated=self.user,
        )
        link.save(user=self.user)
        return link

    def _household(self):
        group = Group(id=uuidlib.uuid4(), code=f"G{uuidlib.uuid4().hex[:8]}",
                      user_created=self.user, user_updated=self.user)
        group.save(user=self.user)
        return group

    def _run(self, *args):
        out = StringIO()
        call_command("backfill_groupindividual_roles", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_without_writing(self):
        group = self._household()
        self._member(group, "1", GroupIndividual.Role.HEAD, 1)
        wrong = self._member(group, "6", GroupIndividual.Role.GRANDSON, 2)

        output = self._run()

        self.assertIn("DRY RUN", output)
        wrong.refresh_from_db()
        self.assertEqual(wrong.role, GroupIndividual.Role.GRANDSON)

    def test_apply_corrects_every_shifted_code(self):
        group = self._household()
        head = self._member(group, "1", GroupIndividual.Role.HEAD, 1)
        cases = [
            (self._member(group, "6", GroupIndividual.Role.GRANDSON, 2),
             GroupIndividual.Role.FATHER),
            (self._member(group, "6", GroupIndividual.Role.GRANDDAUGHTER, 3),
             GroupIndividual.Role.MOTHER),
            (self._member(group, "5", GroupIndividual.Role.BROTHER, 4),
             GroupIndividual.Role.GRANDSON),
            (self._member(group, "5", GroupIndividual.Role.SISTER, 5),
             GroupIndividual.Role.GRANDDAUGHTER),
            (self._member(group, "7", GroupIndividual.Role.FATHER, 6),
             GroupIndividual.Role.FATHER_IN_LAW),
            (self._member(group, "12", GroupIndividual.Role.SPOUSE, 8, gender="F"),
             GroupIndividual.Role.SISTER_IN_LAW),
            (self._member(group, "13", GroupIndividual.Role.OTHER_RELATIVE, 9),
             GroupIndividual.Role.HOUSE_HELP),
            (self._member(group, "11", GroupIndividual.Role.OTHER_RELATIVE, 10,
                          gender="F"),
             GroupIndividual.Role.CO_WIFE),
            (self._member(group, "8", GroupIndividual.Role.OTHER_RELATIVE, 7,
                          gender="M"),
             GroupIndividual.Role.BROTHER),
        ]

        self._run("--apply")

        for link, expected in cases:
            link.refresh_from_db()
            self.assertEqual(link.role, expected)
        head.refresh_from_db()
        self.assertEqual(head.role, GroupIndividual.Role.HEAD)

    def test_correct_rows_are_left_alone(self):
        group = self._household()
        self._member(group, "1", GroupIndividual.Role.HEAD, 1)
        child = self._member(group, "3", GroupIndividual.Role.SON, 2)

        output = self._run("--apply")

        child.refresh_from_db()
        self.assertEqual(child.role, GroupIndividual.Role.SON)
        self.assertIn("already_correct", output)

    def test_stale_json_ext_label_is_rewritten(self):
        group = self._household()
        self._member(group, "1", GroupIndividual.Role.HEAD, 1)
        link = self._member(group, "6", GroupIndividual.Role.GRANDSON, 2,
                            stored_label="GRANDSON")

        self._run("--apply")

        link.individual.refresh_from_db()
        self.assertEqual(link.individual.json_ext["individual_role"], "FATHER")

    def test_rerun_is_idempotent(self):
        group = self._household()
        self._member(group, "1", GroupIndividual.Role.HEAD, 1)
        link = self._member(group, "6", GroupIndividual.Role.GRANDSON, 2)

        self._run("--apply")
        second = self._run("--apply")

        link.refresh_from_db()
        self.assertEqual(link.role, GroupIndividual.Role.FATHER)
        self.assertIn("corrected                        0", second)

    def test_members_without_a_recoverable_code_are_skipped(self):
        group = self._household()
        self._member(group, "1", GroupIndividual.Role.HEAD, 1)
        individual = Individual(
            id=uuidlib.uuid4(), first_name="NoCode", last_name="Test",
            dob="1950-01-01", json_ext={},
            user_created=self.user, user_updated=self.user,
        )
        individual.save(user=self.user)
        link = GroupIndividual(
            id=uuidlib.uuid4(), group=group, individual=individual,
            role=GroupIndividual.Role.BROTHER,
            user_created=self.user, user_updated=self.user,
        )
        link.save(user=self.user)

        self._run("--apply")

        link.refresh_from_db()
        self.assertEqual(link.role, GroupIndividual.Role.BROTHER)
