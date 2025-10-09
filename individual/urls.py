from django.urls import path

from .views import (
    import_individuals,
    download_invalid_items,
    download_individual_upload,
    download_template_file,
    individual_report,
    eligible_households_report,
)

urlpatterns = [
    path('report/', individual_report, name='individual.report'),
    # path('eligible_households_report/', eligible_households_report, name='individual.eligible_households_report'),
    path('import_individuals/', import_individuals, name='import_individuals'),
    path('download_invalid_items/', download_invalid_items),
    path('download_individual_upload_file/', download_individual_upload),
    path('download_template_file/', download_template_file, name='download_template_file'),
]
