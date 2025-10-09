import logging
import json
import mimetypes
import os
import numpy as np
import pandas as pd
from django.db.models import Q
from django.http import StreamingHttpResponse
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from core.utils import DefaultStorageFileHandler
from core.views import check_user_rights
from individual.apps import IndividualConfig
from individual.models import Individual, IndividualDataSource
from individual.services import IndividualImportService
from django.core.files.uploadedfile import InMemoryUploadedFile
from workflow.services import WorkflowService
from faker import Faker
from django.conf import settings
try:
    if 'report' in settings.INSTALLED_APPS:
        from report.services import ReportService  # real service
    else:
        ReportService = None
except Exception:
    ReportService = None

# Set up logging for the module
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".csv", ".xls", ".xlsx"}
ALLOWED_MIME_TYPES = {
    "text/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
}

mimetypes.add_type("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx")
mimetypes.add_type("application/vnd.ms-excel", ".xls")

template = """{
    "docElements": [
        {
            "type": "text",
            "id": 1,
            "content": "Individual Report",
            "fontSize": 18,
            "bold": true,
            "alignment": "center",
            "margin": [0, 0, 0, 20]
        }
    ],
    
}"""

def eligible_households_report(request):
    if ReportService is None:
        # clean response when reporting is disabled
        return JsonResponse(
            {"detail": "Reporting is disabled (report module not installed)."},
            status=501,
        )

    report_service = ReportService(request.user)
    data = {
        "users": [
            {
                "id": fake.uuid4(),
                "email": fake.email(),
                "first_name": fake.first_name(),
                "last_name": fake.last_name(),
            }
            for _ in range(100)
        ]
    }
    return report_service.process("eligible_households_report", data, template)


def individual_report(request):
    if ReportService is None:
        return JsonResponse(
            {"detail": "Reporting is disabled (report module not installed)."},
            status=501,
        )

    # If/when you enable it:
    # report_service = ReportService(request.user)
    # data = {"individuals": [v for v in Individual.get_queryset(None, request.user)]}
    # return report_service.process('individual_report', data, template)
    return JsonResponse({"detail": "Not implemented"}, status=501)


def is_valid_file(import_file):
    """ Validate file extension and MIME type """
    file_extension = os.path.splitext(import_file.name)[1].lower()
    if file_extension not in ALLOWED_EXTENSIONS:
        return False, _("Invalid file type. Allowed: .csv, .xls, .xlsx")

    file_mime_type, _ = mimetypes.guess_type(import_file.name)
    if not file_mime_type:
        return False, _("Could not determine file type")

    if file_mime_type not in ALLOWED_MIME_TYPES:
        return False, _(f"Invalid MIME type:") + f" {file_mime_type}"

    return True, None


# Function to retrieve global schema fields from IndividualConfig
def get_global_schema_fields():
    # Load individual schema as a dictionary
    schema = json.loads(IndividualConfig.individual_schema)
    # Extract property keys and add additional fields specific to individuals
    schema_properties = set(schema.get('properties', {}).keys())
    schema_properties.update(['recipient_info', 'individual_role', 'group_code'])
    return list(schema_properties)

# API endpoint to download a CSV template for individual data import
@api_view(["GET"])
@permission_classes([check_user_rights(IndividualConfig.gql_individual_create_perms)])
def download_template_file(request):
    try:
        # Base fields and extra fields required in the template
        base_fields = IndividualConfig.individual_base_fields
        extra_fields = get_global_schema_fields()
        all_fields = base_fields + extra_fields

        # Create an empty DataFrame with the required fields
        template_df = pd.DataFrame(columns=all_fields)

        # Function to stream the CSV content
        def stream_csv():
            output = template_df.to_csv(index=False)
            yield output.encode('utf-8')

        # Return a streaming HTTP response with the CSV file
        response = StreamingHttpResponse(
            stream_csv(), content_type='text/csv'
        )
        response['Content-Disposition'] = 'attachment; filename="individual_upload_template.csv"'
        return response
    except Exception as exc:
        # Log unexpected errors and return a 500 response
        logger.error("Unexpected error while generating template file", exc_info=exc)
        return Response({'success': False, 'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# API endpoint to import individual data from a file
@api_view(["POST"])
@permission_classes([check_user_rights(IndividualConfig.gql_individual_create_perms)])
def import_individuals(request):
    import_file = None
    try:
        user = request.user
        # Resolve the arguments and handle file upload
        import_file, workflow, group_aggregation_column = _resolve_import_individuals_args(request)

        is_valid, error_message = is_valid_file(import_file)
        if not is_valid:
            return Response({'success': False, 'error': error_message}, status=status.HTTP_400_BAD_REQUEST)

        _handle_file_upload(import_file)
        # Import individual data using the service
        result = IndividualImportService(user).import_individuals(import_file, workflow, group_aggregation_column)

        # If the import was unsuccessful, raise an error
        if not result.get('success'):
            raise ValueError('{}: {}'.format(result.get("message"), result.get("details")))

        # Return the result of the import
        return Response(result)
    except ValueError as e:
        # Remove the file and log the error if a value error occurs
        if import_file:
            _remove_file(import_file)
        logger.error("Error while uploading individuals", exc_info=e)
        return Response({'success': False, 'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except FileExistsError as e:
        # Handle file existence conflicts
        logger.error("Error while saving file", exc_info=e)
        return Response({'success': False, 'error': str(e)}, status=status.HTTP_409_CONFLICT)
    except Exception as e:
        # Handle unexpected errors and return a 500 response
        logger.error("Unexpected error while uploading individuals", exc_info=e)
        return Response({'success': False, 'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# API endpoint to download invalid items from an individual data upload
@api_view(["GET"])
@permission_classes([check_user_rights(IndividualConfig.gql_individual_search_perms)])
def download_invalid_items(request):
    try:
        # Get the upload ID from the request parameters
        upload_id = request.query_params.get('upload_id')

        # Query invalid items from the data source based on the upload ID
        invalid_items = IndividualDataSource.objects.filter(
            Q(is_deleted=False) &
            Q(upload_id=upload_id) &
            ~Q(validations__validation_errors=[])
        )

        # Prepare data for invalid items as a list of dictionaries
        data_from_source = []
        for invalid_item in invalid_items:
            json_ext = invalid_item.json_ext
            json_ext["id"] = invalid_item.id
            json_ext["error"] = invalid_item.validations
            data_from_source.append(json_ext)

        # Convert the data into a DataFrame
        recreated_df = pd.DataFrame(data_from_source)

        # Stream the DataFrame content as a CSV file
        def stream_csv():
            output = recreated_df.to_csv(index=False)
            yield output.encode('utf-8')

        # Return a streaming HTTP response with the CSV file
        response = StreamingHttpResponse(
            stream_csv(), content_type='text/csv'
        )
        response['Content-Disposition'] = 'attachment; filename="individuals_invalid_items.csv"'
        return response

    except ValueError as exc:
        # Log value errors and return a 400 response
        logger.error("Error while fetching data", exc_info=exc)
        return Response({'success': False, 'error': str(exc)}, status=400)
    except Exception as exc:
        # Handle unexpected errors and return a 500 response
        logger.error("Unexpected error", exc_info=exc)
        return Response({'success': False, 'error': str(exc)}, status=500)

# API endpoint to download a previously uploaded individual data file
@api_view(["GET"])
@permission_classes([check_user_rights(IndividualConfig.gql_individual_search_perms)])
def download_individual_upload(request):
    try:
        # Get the filename from the request parameters
        filename = request.query_params.get('filename')
        target_file_path = IndividualConfig.get_individual_upload_file_path(filename)

        # Create a file handler to manage the file
        file_handler = DefaultStorageFileHandler(target_file_path)
        return file_handler.get_file_response_csv(filename)

    except ValueError as exc:
        logger.error("Error while fetching data", exc_info=exc)
        return Response({'success': False, 'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except FileNotFoundError as exc:
        logger.error("Error while getting file", exc_info=exc)
        return Response({'success': False, 'error': str(exc)}, status=status.HTTP_404_NOT_FOUND)
    except Exception as exc:
        logger.error("Unexpected error", exc_info=exc)
        return Response({'success': False, 'error': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

# Function to handle file uploads and save them to a specified path
def _handle_file_upload(file):
    try:
        target_file_path = IndividualConfig.get_individual_upload_file_path(file.name)
        file_handler = DefaultStorageFileHandler(target_file_path)
        file_handler.save_file(file)
    except FileExistsError as exc:
        raise exc

# Function to remove a file from storage
def _remove_file(file):
    target_file_path = IndividualConfig.get_individual_upload_file_path(file.name)
    file_handler = DefaultStorageFileHandler(target_file_path)
    file_handler.remove_file()

# Helper function to resolve and validate import arguments from the request
def _resolve_import_individuals_args(request):
    import_file = request.FILES.get('file')
    workflow_name = request.POST.get('workflow_name')
    workflow_group = request.POST.get('workflow_group')
    group_aggregation_column = request.POST.get('group_aggregation_column')

    # Validate the presence of required arguments
    if not import_file:
        raise ValueError('Import file not provided')
    if not workflow_name:
        raise ValueError('Workflow name not provided')
    if not workflow_group:
        raise ValueError('Workflow group not provided')

    # Retrieve workflows based on the provided arguments
    result = WorkflowService.get_workflows(workflow_name, workflow_group)
    if not result.get('success'):
        raise ValueError('{}: {}'.format(result.get("message"), result.get("details")))

    workflows = result.get('data', {}).get('workflows')
    if not workflows:
        raise ValueError('Workflow not found: group={} name={}'.format(workflow_group, workflow_name))
    if len(workflows) > 1:
        raise ValueError('Multiple workflows found: group={} name={}'.format(workflow_group, workflow_name))

    # Return the resolved import file, workflow, and aggregation column
    return import_file, workflows[0], group_aggregation_column
