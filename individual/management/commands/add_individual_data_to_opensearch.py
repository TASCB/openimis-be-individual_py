from django.core.management.base import BaseCommand
from individual.models import Individual
from individual.documents import IndividualDocument


class Command(BaseCommand):
    help = (
        "Imports Individual data into OpenSearch. "
        "Run with: python manage.py add_individual_data_to_opensearch"
    )

    def handle(self, *args, **options):
        # Initialize the index
        IndividualDocument.init(index='individual')

        # Loop through all Individual objects
        for obj in Individual.objects.all():
            doc = IndividualDocument(
                meta={'id': obj.id},  # set document ID
                first_name=obj.first_name,
                last_name=obj.last_name,
                dob=obj.dob,
                date_created=obj.date_created,
                json_ext=obj.json_ext,
            )

            result = doc.save()  # save to OpenSearch
            self.stdout.write(self.style.SUCCESS(f'Indexed Individual {obj.id}: {result}'))

        self.stdout.write(self.style.SUCCESS("All Individual records indexed successfully."))
