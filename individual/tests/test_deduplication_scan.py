import uuid
from datetime import date
from unittest import mock

from django.test import TestCase

from individual.apps import IndividualConfig
from individual.models import Individual
from individual.services import (
    CreateDeduplicationIndividualReviewTasksService, DeduplicationFieldError,
    get_individual_duplication_aggregation,
)


class DeduplicationScanTest(TestCase):
    def setUp(self):
        from core.models import User
        self.user = User.objects.filter(username='Admin').first()
        if self.user is None:
            self.skipTest('needs the Admin user')
        self.name = f'Dedup{uuid.uuid4().hex[:10]}'

    def person(self, location=None, **json_ext):
        individual = Individual(first_name=self.name, last_name='Test', dob=date(1990, 1, 1), json_ext=json_ext,
                                location=location)
        individual.save(username=self.user.username)
        return individual

    def scan(self, *columns, location=None):
        result = get_individual_duplication_aggregation(['first_name', *columns], location)
        return [r for r in result['rows'] if r['column_values']['first_name'] == self.name]

    def test_extra_field_matches_ignoring_case_and_spaces(self):
        a, b = self.person(external_id=' AB-1 '), self.person(external_id='ab-1')
        self.person(external_id='AB-2')
        rows = self.scan('external_id')
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(rows[0]['ids']), sorted([str(a.id), str(b.id)]))
        self.assertEqual(rows[0]['column_values']['external_id'], 'ab-1')

    def test_blank_values_never_match(self):
        self.person(external_id=''), self.person(external_id='  '), self.person()
        self.assertEqual(self.scan('external_id'), [])

    def test_unknown_field_is_an_error(self):
        with self.assertRaises(DeduplicationFieldError):
            get_individual_duplication_aggregation(['national_id'])

    def test_scan_returns_at_most_the_limit_and_the_full_total(self):
        self.person(), self.person()
        with mock.patch.object(IndividualConfig, 'deduplication_max_groups', 1):
            result = get_individual_duplication_aggregation(['last_name', 'dob'])
        self.assertEqual(len(result['rows']), 1)
        self.assertGreaterEqual(result['total'], 1)
        self.assertEqual(result['limit'], 1)

    def test_more_groups_than_the_limit_make_no_tasks(self):
        groups = [{'count': 2, 'ids': [str(uuid.uuid4()), str(uuid.uuid4())]} for _ in range(2)]
        with mock.patch.object(IndividualConfig, 'deduplication_max_groups', 1), \
                mock.patch('tasks_management.services.TaskService.create') as create:
            result = CreateDeduplicationIndividualReviewTasksService(self.user).create_individual_duplication_tasks(groups)
        self.assertFalse(result['success'])
        create.assert_not_called()

    def test_location_at_any_level_scans_only_people_under_it(self):
        from location.models import Location
        village = Location.objects.filter(type='V', validity_to__isnull=True,
                                          parent__parent__parent__isnull=False).first()
        other_region = Location.objects.filter(type='R', validity_to__isnull=True).exclude(
            id=village.parent.parent.parent_id).first() if village else None
        if not village or not other_region:
            self.skipTest('needs a village and a second region')
        self.person(village), self.person(village)
        for place in (village, village.parent, village.parent.parent, village.parent.parent.parent):
            self.assertEqual(len(self.scan(location=str(place.uuid))), 1, place.type)
            self.assertEqual(len(self.scan(location=uuid.UUID(place.uuid))), 1, f'{place.type} as UUID')
        self.assertEqual(self.scan(location=str(other_region.uuid)), [])
