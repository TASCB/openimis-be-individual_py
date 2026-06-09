from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from core.test_helpers import LogInHelper
from individual.models import Group, PmtEnrollment
from individual.pmt_service import PctAutoEnrollmentService


class _FakeQuerySet(list):
    def select_related(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self


class _FakeGroupBeneficiaryManager:
    def __init__(self, existing=None):
        self.items = list(existing or [])
        self._next_id = max([getattr(item, 'id', 0) for item in self.items] or [0]) + 1

    def filter(self, **kwargs):
        result = []
        for item in self.items:
            if kwargs.get('group_id__in') and item.group_id not in kwargs['group_id__in']:
                continue
            if 'benefit_plan' in kwargs and item.benefit_plan != kwargs['benefit_plan']:
                continue
            if 'is_deleted' in kwargs and getattr(item, 'is_deleted', False) != kwargs['is_deleted']:
                continue
            result.append(item)
        return _FakeQuerySet(result)

    def add(self, item):
        if getattr(item, 'id', None) is None:
            item.id = self._next_id
            self._next_id += 1
        self.items.append(item)


class _FakeGroupBeneficiary:
    objects = _FakeGroupBeneficiaryManager()

    def __init__(self, group, benefit_plan, status, json_ext=None):
        self.id = None
        self.group = group
        self.group_id = group.id
        self.benefit_plan = benefit_plan
        self.status = status
        self.json_ext = json_ext or {}
        self.is_deleted = False

    def save(self, user=None, username=None):
        if self not in self.__class__.objects.items:
            self.__class__.objects.add(self)


class PctAutoEnrollmentServiceTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = LogInHelper().get_or_create_user_api(username='pctsync')

    def _create_group(self, code):
        group = Group(code=code)
        group.save(user=self.user)
        return group

    def _create_enrollment(self, group, score='9.500'):
        enrollment = PmtEnrollment(
            group=group,
            pmt_class=PmtEnrollment.PmtClass.POOR,
            pmt_score=score,
            status=PmtEnrollment.Status.PENDING,
            json_ext={},
        )
        enrollment.save(user=self.user)
        return enrollment

    def test_sync_creates_and_links_group_beneficiaries_once(self):
        group_a = self._create_group('PCT-A')
        group_b = self._create_group('PCT-B')
        enrollment_a = self._create_enrollment(group_a)
        enrollment_b = self._create_enrollment(group_b)

        manager = _FakeGroupBeneficiaryManager()
        FakeGB = type('FakeGB', (_FakeGroupBeneficiary,), {'objects': manager})
        benefit_plan = SimpleNamespace(code='PCT')

        with patch('individual.pmt_service.IndividualConfig.pct_auto_enroll_enabled', True), \
             patch('individual.pmt_service.IndividualConfig.pct_group_beneficiary_status', 'ACTIVE'), \
             patch.object(PctAutoEnrollmentService, '_resolve_social_protection_dependencies', return_value={'BenefitPlan': object, 'GroupBeneficiary': FakeGB}), \
             patch.object(PctAutoEnrollmentService, '_resolve_pct_benefit_plan', return_value=benefit_plan):
            service = PctAutoEnrollmentService(self.user)
            result = service.sync_pending_poor_households()
            self.assertTrue(result['success'])
            self.assertEqual(result['created'], 2)
            self.assertEqual(result['linked'], 2)
            self.assertEqual(len(manager.items), 2)

            enrollment_a.refresh_from_db()
            enrollment_b.refresh_from_db()
            self.assertEqual(enrollment_a.status, PmtEnrollment.Status.ENROLLED)
            self.assertEqual(enrollment_b.status, PmtEnrollment.Status.ENROLLED)
            self.assertIsNotNone(enrollment_a.beneficiary_id)
            self.assertIsNotNone(enrollment_b.beneficiary_id)

            second = service.sync_pending_poor_households()
            self.assertTrue(second['success'])
            self.assertEqual(second['processed'], 0)
            self.assertEqual(second['created'], 0)
            self.assertEqual(len(manager.items), 2)

    def test_sync_reuses_existing_group_beneficiary(self):
        group = self._create_group('PCT-EXISTING')
        enrollment = self._create_enrollment(group, score='8.250')
        benefit_plan = SimpleNamespace(code='PCT')
        existing = _FakeGroupBeneficiary(group=group, benefit_plan=benefit_plan, status='SUSPENDED', json_ext={})
        manager = _FakeGroupBeneficiaryManager(existing=[existing])
        FakeGB = type('FakeGB', (_FakeGroupBeneficiary,), {'objects': manager})
        existing.__class__ = FakeGB
        manager.items = [existing]

        with patch('individual.pmt_service.IndividualConfig.pct_auto_enroll_enabled', True), \
             patch('individual.pmt_service.IndividualConfig.pct_group_beneficiary_status', 'ACTIVE'), \
             patch.object(PctAutoEnrollmentService, '_resolve_social_protection_dependencies', return_value={'BenefitPlan': object, 'GroupBeneficiary': FakeGB}), \
             patch.object(PctAutoEnrollmentService, '_resolve_pct_benefit_plan', return_value=benefit_plan):
            result = PctAutoEnrollmentService(self.user).sync_pending_poor_households()
            self.assertTrue(result['success'])
            self.assertEqual(result['created'], 0)
            self.assertEqual(result['updated'], 1)
            self.assertEqual(result['linked'], 1)
            self.assertEqual(len(manager.items), 1)
            self.assertEqual(existing.status, 'ACTIVE')

            enrollment.refresh_from_db()
            self.assertEqual(enrollment.status, PmtEnrollment.Status.ENROLLED)
            self.assertEqual(enrollment.beneficiary_id, existing.id)
