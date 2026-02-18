from django.core.management.base import BaseCommand
from individual.models import GroupIndividual
from individual.documents import GroupIndividualDocument


class Command(BaseCommand):
    help = (
        "Imports GroupIndividual data into OpenSearch. "
        "Run with: python manage.py add_groupindividual_data_to_opensearch"
    )

    def handle(self, *args, **options):
        # Initialize the index
        GroupIndividualDocument.init(index='group_individual')

        # Loop through all GroupIndividual objects
        for obj in GroupIndividual.objects.all():
            doc = GroupIndividualDocument(
                meta={'id': obj.id},  # set document ID
                group={
                    "id": obj.group.id,
                    "code": obj.group.code,
                    "json_ext": obj.group.json_ext,
                },
                individual={
                    "first_name": obj.individual.first_name,
                    "last_name": obj.individual.last_name,
                    "dob": obj.individual.dob,
                },
                role=obj.role,
                recipient_type=obj.recipient_type,
                json_ext=obj.json_ext,
            )

            result = doc.save()  # save to OpenSearch
            self.stdout.write(self.style.SUCCESS(f'Indexed GroupIndividual {obj.id}: {result}'))

        self.stdout.write(self.style.SUCCESS("All GroupIndividual records indexed successfully."))
