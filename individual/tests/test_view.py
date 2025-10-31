import os
from unittest.mock import patch, MagicMock
from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from core.test_helpers import create_test_interactive_user
from core.models import ModuleConfiguration


class TestView(APITestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_file_path = os.path.join(
            os.path.dirname(__file__), 'fixtures', 'individual_upload.csv'
        )
        cls.test_config_path = os.path.join(
            os.path.dirname(__file__), 'fixtures', 'individual_config.json'
        )

    def setUp(self):
        self.admin_user = create_test_interactive_user()

    def test_download_template_file(self):
        self.client.force_authenticate(user=self.admin_user)
        url = reverse('download_template_file')
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response['Content-Type'], 'text/csv')
        self.assertIn(
            'attachment; filename="individual_upload_template.csv"',
            response['Content-Disposition']
        )

        expected_base_csv_header = f'first_name,last_name,dob,location_name,location_code,id'
        content = b"".join(response.streaming_content).decode('utf-8')
        self.assertTrue(
            expected_base_csv_header in content,
            f'Expect csv template header to contain {expected_base_csv_header}, but got {content}'
        )

    def test_download_template_file_on_individual_schema_update(self):
        # First set the individual config to be empty
        config = ModuleConfiguration.objects.filter(module='individual', layer='be')
        if not config:
            config = ModuleConfiguration(module='individual', layer='be', config='{}')
        else:
            config.config = '{}'
        config.save()

        # Then update individual config to with the fixture config
        with open(self.test_config_path, 'rb') as test_file:
            config.config = test_file.read()
        config.save()

        self.client.force_authenticate(user=self.admin_user)
        url = reverse('download_template_file')
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check the template file contains individual schema fields from fixture config
        content = b"".join(response.streaming_content).decode('utf-8')
        for expected_column in ('poor', 'educated_level', 'number_of_children'):
            self.assertTrue(
                expected_column in content,
                f'Expect csv template header to contain {expected_column}, but got {content}'
            )

    @patch('individual.views.WorkflowService')
    @patch('individual.views.IndividualImportService')
    @patch('individual.views.DefaultStorageFileHandler')
    def test_import_individuals_success(
            self, mock_file_handler, mock_individual_import_service, mock_workflow_service):

        self.client.force_authenticate(user=self.admin_user)

        mock_workflow_service.get_workflows.return_value = {
            'success': True,
            'data': {
                'workflows': [{'id': 1, 'name': 'Test Workflow'}]
            }
        }

        mock_individual_import_service.return_value.import_individuals.return_value = {
            'success': True,
            'message': 'Import successful',
            'details': None
        }

        mock_handler_instance = MagicMock()
        mock_file_handler.return_value = mock_handler_instance

        with open(self.test_file_path, 'rb') as test_file:
            response = self.client.post(
                reverse('import_individuals'),
                data={
                    'file': test_file,
                    'workflow_name': 'Test Workflow',
                    'workflow_group': 'Test Group',
                    'group_aggregation_column': 'group_code'
                },
                format='multipart'
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['success'], True)
        self.assertIn('message', response.data)

        mock_handler_instance.save_file.assert_called_once()

    @patch('individual.views.WorkflowService')
    @patch('individual.views.IndividualImportService')
    @patch('individual.views.DefaultStorageFileHandler')
    def test_import_individuals_import_service_failure(
            self, mock_file_handler, mock_individual_import_service, mock_workflow_service):

        self.client.force_authenticate(user=self.admin_user)

        mock_handler_instance = MagicMock()
        mock_file_handler.return_value = mock_handler_instance

        mock_individual_import_service.return_value.import_individuals.return_value = {
            'success': False,
            'message': 'Import service not available',
            'details': 'Dummy import service issue'
        }

        mock_workflow_service.get_workflows.return_value = {
            'success': True,
            'data': {
                'workflows': [{'id': 1, 'name': 'Test Workflow'}]
            }
        }

        with open(self.test_file_path, 'rb') as test_file:
            response = self.client.post(
                reverse('import_individuals'),
                data={
                    'file': test_file,
                    'workflow_name': 'Test Workflow',
                    'workflow_group': 'Test Group',
                    'group_aggregation_column': 'group_code'
                },
                format='multipart'
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)

        mock_handler_instance.save_file.assert_called_once()
        mock_handler_instance.remove_file.assert_called_once()


    @patch('individual.views.WorkflowService')
    @patch('individual.views.DefaultStorageFileHandler')
    def test_import_individuals_workflow_service_failure(
            self, mock_file_handler, mock_workflow_service):

        self.client.force_authenticate(user=self.admin_user)

        mock_handler_instance = MagicMock()
        mock_file_handler.return_value = mock_handler_instance

        mock_workflow_service.get_workflows.return_value = {
            'success': False,
            'message': 'Workflow service not available',
            'details': 'Dummy workflow service issue'
        }

        with open(self.test_file_path, 'rb') as test_file:
            response = self.client.post(
                reverse('import_individuals'),
                data={
                    'file': test_file,
                    'workflow_name': 'Test Workflow',
                    'workflow_group': 'Test Group',
                    'group_aggregation_column': 'group_code'
                },
                format='multipart'
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('error', response.data)

        mock_handler_instance.save_file.assert_not_called()
        mock_handler_instance.remove_file.assert_not_called()
