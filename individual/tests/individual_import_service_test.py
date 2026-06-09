import csv
import json
import os
import pandas as pd
import uuid
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.query import QuerySet
from django.test import TestCase
from individual.services import IndividualImportService
from individual.models import (
    Individual,
    IndividualDataSource,
    IndividualDataSourceUpload,
    IndividualDataUploadRecords,
    GroupIndividual,
)
from individual.tests.test_helpers import (
    generate_random_string,
    create_individual,
    create_sp_role,
    create_test_village,
    create_test_interactive_user,
    assign_user_districts,
)
from opensearch_reports.service import BaseSyncDocument
from unittest.mock import MagicMock, patch
from core.utils import filter_validity
from core.models.user import Role
from location.models import Location


def count_csv_records(file_path):
    with open(file_path, mode='r', encoding='utf-8') as file:
        reader = csv.reader(file)
        valid_rows = list(
            row for row in reader 
            if any(cell.strip() for cell in row)  # Do not count blank lines
        )
        return len(valid_rows) - 1  # Exclude the header row


class IndividualImportServiceTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        admin_role = Role.objects.filter(is_system=64, *filter_validity()).first()
        cls.admin_user = create_test_interactive_user(
            username="superuserme", roles=[admin_role.id]
        )

        cls.csv_file_path = os.path.join(os.path.dirname(__file__), 'fixtures', 'individual_upload.csv')
        # SimpleUploadedFile requires a bytes-like object so use 'rb' instead of 'r'
        with open(cls.csv_file_path, 'rb') as f:
            cls.csv_content = f.read()

        cls.village_a = create_test_village({
            'name': 'Washington DC',
            'code': '202',
        })
        cls.village_b = create_test_village({
            'name': 'Fairfax',
            'code': '703'
        })

        cls.upload = IndividualDataSourceUpload(
            source_name="Test Upload.csv",
            source_type="individual import",
        )
        cls.upload.save(user=cls.admin_user)
        cls.individual1 = create_individual(cls.admin_user.username)
        cls.individual2 = create_individual(cls.admin_user.username)

        source1 = IndividualDataSource(individual=cls.individual1, upload=cls.upload)
        source1.save(user=cls.admin_user)
        source2 = IndividualDataSource(individual=cls.individual2, upload=cls.upload)
        source2.save(user=cls.admin_user)

        cls.empty_upload = IndividualDataSourceUpload(
            source_name="no-data.csv",
            source_type="individual import",
        )
        cls.empty_upload.save(user=cls.admin_user)


    def test_import_individuals(self):
        uploaded_csv_name = f"{generate_random_string(20)}.csv"
        csv_file = SimpleUploadedFile(
            uploaded_csv_name,
            self.csv_content,
            content_type="text/csv"
        )

        mock_workflow = self._create_mock_workflow()

        service = IndividualImportService(self.admin_user)
        result = service.import_individuals(csv_file, mock_workflow, "group_code")
        self.assertEqual(result['success'], True)

        # Check that an IndividualDataSourceUpload object was saved in the database
        upload = IndividualDataSourceUpload.objects.get(source_name=uploaded_csv_name)
        self.assertIsNotNone(upload)
        self.assertEqual(upload.source_name, uploaded_csv_name)
        self.assertEqual(upload.source_type, "individual import")
        self.assertEqual(upload.status, IndividualDataSourceUpload.Status.TRIGGERED)

        self.assertEqual(result['data']['upload_uuid'], upload.uuid)

        # Check that an IndividualDataUploadRecords object was saved in the database
        data_upload_record = IndividualDataUploadRecords.objects.get(data_upload=upload)
        self.assertIsNotNone(data_upload_record)
        self.assertEqual(data_upload_record.workflow, mock_workflow.name)
        self.assertEqual(data_upload_record.json_ext['group_aggregation_column'], "group_code")

        # Check that an IndividualDataSource objects saved in the database
        individual_data_sources = IndividualDataSource.objects.filter(upload=upload)
        num_records = count_csv_records(self.csv_file_path)
        self.assertEqual(individual_data_sources.count(), num_records)

        # Check that workflow is triggered
        mock_workflow.run.assert_called_once_with({
            'user_uuid': str(self.admin_user.id),
            'upload_uuid': str(upload.uuid),
        })


    def _create_mock_workflow(self):
        mock_workflow = MagicMock()
        mock_workflow.name = 'Test Workflow'
        return mock_workflow

    @patch('individual.services.load_dataframe')
    @patch('individual.services.fetch_summary_of_broken_items')
    def test_validate_import_individuals_success(self, mock_fetch_summary, mock_load_dataframe):
        upload_id = uuid.uuid4()

        dataframe = pd.DataFrame({
            'id': [1, 2],
            'first_name': ['John', 'Jane'],
            'last_name': ['Doe', 'Smith'],
            'email': ['john@example.com', 'jane@example.com']
        })
        mock_load_dataframe.return_value = dataframe

        mock_invalid_items = {"invalid_items_count": 0}
        mock_fetch_summary.return_value = mock_invalid_items

        individual_sources = MagicMock()
        service = IndividualImportService(self.admin_user)
        result = service.validate_import_individuals(upload_id, individual_sources)

        mock_load_dataframe.assert_called_once_with(individual_sources)

        # Assert that the result contains the validated dataframe and summary of invalid items
        self.assertEqual(result['success'], True)
        self.assertEqual(len(result['data']), 2)  # Two records were validated
        self.assertEqual(result['summary_invalid_items'], mock_invalid_items)

        # Check the validation logic on the dataframe
        validated_rows = result['data']
        for row in validated_rows:
            self.assertIn('validations', row)
            self.assertTrue(all(v.get('success', True) for v in row['validations'].values()))

    @patch('individual.services.IndividualConfig.individual_schema', json.dumps({
        "properties": {
            "email": {"type": "string", "uniqueness": True}
        }
    }))  # Mock schema for testing uniqueness
    @patch('individual.services.load_dataframe')
    @patch('individual.services.fetch_summary_of_broken_items')
    def test_validate_import_individuals_with_duplicate_emails(self, mock_fetch_summary, mock_load_dataframe):
        upload_id = uuid.uuid4()

        # Create a dataframe with duplicate emails to test uniqueness validation
        email = 'john@example.com'
        dataframe = pd.DataFrame({
            'id': [1, 2],
            'email': [email, email]
        })
        mock_load_dataframe.return_value = dataframe

        mock_invalid_items = {"invalid_items_count": 1}
        mock_fetch_summary.return_value = mock_invalid_items

        individual_sources = MagicMock()
        service = IndividualImportService(self.admin_user)
        result = service.validate_import_individuals(upload_id, individual_sources)

        mock_load_dataframe.assert_called_once_with(individual_sources)

        # Assert that the result contains the validated dataframe and summary of invalid items
        self.assertEqual(result['success'], True)
        self.assertEqual(len(result['data']), 2)  # Two records were validated
        self.assertEqual(result['summary_invalid_items'], mock_invalid_items)

        # Check that the validation flagged the duplicate emails
        validated_rows = result['data']
        for row in validated_rows:
            if row['row']['email'] == email:
                self.assertIn('validations', row)
                email_validation = row['validations']['email_uniqueness']
                self.assertFalse(email_validation.get('success'))
                self.assertEqual(email_validation.get('field_name'), 'email')
                self.assertEqual(email_validation.get('note'), "'email' Field value 'john@example.com' is duplicated")


    @patch('individual.services.load_dataframe')
    @patch('individual.services.fetch_summary_of_broken_items')
    def test_validate_import_individuals_row_level_security(self, mock_fetch_summary, mock_load_dataframe):
        # set up a user assigned the district village_a is in
        sp_role = create_sp_role(self.admin_user)
        dist_a_user = create_test_interactive_user(
            username="districtAUserS", roles=[sp_role.id])
        district_a_code = self.village_a.parent.parent.code
        assign_user_districts(dist_a_user, ["R1D1", district_a_code])

        dataframe = pd.read_csv(self.csv_file_path, na_filter=False)
        dataframe['id'] = dataframe.index+1
        mock_load_dataframe.return_value = dataframe

        mock_invalid_items = {"invalid_items_count": 2}
        mock_fetch_summary.return_value = mock_invalid_items

        upload_id = uuid.uuid4()
        individual_sources = MagicMock()

        service = IndividualImportService(dist_a_user)
        result = service.validate_import_individuals(upload_id, individual_sources)

        mock_load_dataframe.assert_called_once_with(individual_sources)

        # Assert that the result contains the validated dataframe and summary of invalid items
        self.assertEqual(result['success'], True)
        self.assertEqual(len(result['data']), dataframe.shape[0])
        self.assertEqual(result['summary_invalid_items'], mock_invalid_items)

        # Check that the validation flagged lack of permission and unrecognized locations
        validated_rows = result['data']

        # User from district a can import individuals from village a
        rows_village_a = [row for row in validated_rows if row['row']['location_name'] == self.village_a.name]
        self.assertTrue(rows_village_a, f'Expected at least one row with location_name={self.village_a.name}')
        for row in rows_village_a:
            loc_validation = row['validations']['location_name']
            self.assertTrue(
                loc_validation.get('success'),
                f'Expected rows with location_name={self.village_a.name} to pass validation, but failed: {loc_validation}'
            )
            self.assertEqual(loc_validation.get('field_name'), 'location_name')

    def test_link_groups_for_upload_uuid_processes_head_before_other_members(self):
        service = IndividualImportService(self.admin_user)
        upload = IndividualDataSourceUpload(
            source_name="ordered-household.csv",
            source_type="individual import",
        )
        upload.save(user=self.admin_user)

        group_code = "P3-TEST-ORDER"

        child = create_individual(self.admin_user.username, {
            "first_name": "Child",
            "last_name": "Member",
            "location": self.village_a,
            "json_ext": {
                "group_code": group_code,
                "individual_role": "OTHER RELATIVE",
                "individual_role_code": "3",
                "hhrep": "2",
                "member_ordinal": 3,
            },
        })
        spouse = create_individual(self.admin_user.username, {
            "first_name": "Spouse",
            "last_name": "Member",
            "location": self.village_a,
            "json_ext": {
                "group_code": group_code,
                "individual_role": "SPOUSE",
                "individual_role_code": "2",
                "hhrep": "2",
                "member_ordinal": 2,
            },
        })
        head = create_individual(self.admin_user.username, {
            "first_name": "Head",
            "last_name": "Member",
            "location": self.village_a,
            "json_ext": {
                "group_code": group_code,
                "individual_role": "HEAD",
                "individual_role_code": "1",
                "hhrep": "2",
                "member_ordinal": 1,
            },
        })

        for individual in (child, spouse, head):
            IndividualDataSource(
                individual=individual,
                upload=upload,
            ).save(user=self.admin_user)

        result = service.link_groups_for_upload_uuid(str(upload.uuid))
        self.assertTrue(result["success"])

        child_link = GroupIndividual.objects.get(individual=child, is_deleted=False)
        spouse_link = GroupIndividual.objects.get(individual=spouse, is_deleted=False)
        head_link = GroupIndividual.objects.get(individual=head, is_deleted=False)

        self.assertEqual(head_link.role, GroupIndividual.Role.HEAD)
        self.assertEqual(spouse_link.role, GroupIndividual.Role.SPOUSE)
        self.assertEqual(spouse_link.recipient_type, GroupIndividual.RecipientType.PRIMARY)
        self.assertEqual(child_link.role, GroupIndividual.Role.OTHER_RELATIVE)
        self.assertIsNone(child_link.recipient_type)

        # User from district a can import individuals without location specified
        rows_empty_location = [row for row in validated_rows if row['row']['location_name'] == '']
        self.assertTrue(rows_empty_location, 'Expected at least one row with location_name=""')
        for row in rows_empty_location:
            loc_validation = row['validations']['location_name']
            self.assertTrue(
                loc_validation.get('success'),
                'Expected rows with empty location_name to pass validation, but failed: {loc_validation}'
            )
            self.assertEqual(loc_validation.get('field_name'), 'location_name')

        # User from district a cannot import individuals from village b
        rows_village_b = [row for row in validated_rows if row['row']['location_name'] == self.village_b.name]
        self.assertTrue(rows_village_b, f'Expected at least one row with location_name={self.village_b.name}')
        for row in rows_village_b:
            loc_validation = row['validations']['location_name']
            self.assertFalse(loc_validation.get('success'))
            self.assertEqual(loc_validation.get('field_name'), 'location_name')
            self.assertEqual(
                loc_validation.get('note'),
                f"Location with name '{self.village_b.name}' and code '{self.village_b.code}' is outside the current user's location permissions."
            )

        # Unknown individual location
        rows_unknown_loc = [row for row in validated_rows if row['row']['location_name'] == 'Washington D.C.']
        self.assertTrue(rows_unknown_loc, 'Expected at least one row with location_name="Washington D.C."')
        for row in rows_unknown_loc:
            loc_validation = row['validations']['location_name']
            self.assertFalse(loc_validation.get('success'))
            self.assertEqual(loc_validation.get('field_name'), 'location_name')
            self.assertEqual(
                loc_validation.get('note'),
                "Location with name 'Washington D.C.' and code '202' is not valid. "
                "Please check the spelling against the list of locations in the system."
            )


    @patch('individual.services.load_dataframe')
    @patch('individual.services.fetch_summary_of_broken_items')
    def test_validate_import_individuals_ambiguous_location_name(self, mock_fetch_summary, mock_load_dataframe):
        # set up another location with the same named and code in DB
        loc_dup = Location.objects.create(**{
            'name': 'Fairfax',
            'code': '703',
            'type': 'V',
            'parent': self.village_b.parent,
        })

        dataframe = pd.read_csv(self.csv_file_path, na_filter=False)
        dataframe['id'] = dataframe.index+1
        mock_load_dataframe.return_value = dataframe

        mock_invalid_items = {"invalid_items_count": 2}
        mock_fetch_summary.return_value = mock_invalid_items

        upload_id = uuid.uuid4()
        individual_sources = MagicMock()

        service = IndividualImportService(self.admin_user)
        result = service.validate_import_individuals(upload_id, individual_sources)

        mock_load_dataframe.assert_called_once_with(individual_sources)

        # Assert that the result contains the validated dataframe and summary of invalid items
        self.assertEqual(result['success'], True)
        self.assertEqual(len(result['data']), dataframe.shape[0])
        self.assertEqual(result['summary_invalid_items'], mock_invalid_items)

        # Check that the validation flagged lack of permission and unrecognized locations
        validated_rows = result['data']

        rows_ambiguous_loc = [row for row in validated_rows if row['row']['location_name'] == loc_dup.name]
        self.assertTrue(rows_ambiguous_loc, f'Expected at least one row with location_name={loc_dup.name}')
        for row in rows_ambiguous_loc:
            loc_validation = row['validations']['location_name']
            self.assertFalse(loc_validation.get('success'))
            self.assertEqual(loc_validation.get('field_name'), 'location_name')
            self.assertEqual(
                loc_validation.get('note'),
                "Location with name 'Fairfax' and code '703' is ambiguous, "
                "because there are more than one location with this name and code found in the system."
            )

    @patch.object(BaseSyncDocument, 'update')
    def test_synchronize_data_for_reporting(self, mock_update):
        service = IndividualImportService(self.admin_user)
        service.synchronize_data_for_reporting(self.upload.id)

        mock_update.assert_called()
        args, kwargs = mock_update.call_args
        self.assertIsInstance(args[0], QuerySet)
        actual_sorted_pks = sorted(list(args[0].values_list('pk', flat=True)))
        expected_sorted_pks = sorted([self.individual1.pk, self.individual2.pk])
        self.assertEqual(actual_sorted_pks, expected_sorted_pks)
        self.assertEqual(args[1], 'index')

    @patch.object(BaseSyncDocument, 'update')
    def test_synchronize_data_for_reporting_no_individuals(self, mock_update):
        service = IndividualImportService(self.admin_user)
        service.synchronize_data_for_reporting(self.empty_upload.id)

        mock_update.assert_not_called()
