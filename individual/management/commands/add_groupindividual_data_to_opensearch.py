from django.core.management.base import BaseCommand
from individual.models import GroupIndividual
from individual.documents import GroupIndividualDocument


class Command(BaseCommand):
    help = (
        "Imports GroupIndividual data into OpenSearch. "
        "Run with: python manage.py add_groupindividual_data_to_opensearch [--skip-errors] [--limit N]"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--skip-errors',
            action='store_true',
            help='Skip records that fail indexing instead of crashing'
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Limit number of records to index'
        )

    def handle(self, *args, **options):
        skip_errors = options.get('skip_errors', False)
        limit = options.get('limit')

        # Initialize the index
        self.stdout.write(self.style.SUCCESS("Initializing index..."))
        GroupIndividualDocument.init(index='group_individual')

        # Get queryset
        queryset = GroupIndividual.objects.all()
        if limit:
            queryset = queryset[:limit]

        total = queryset.count()
        indexed = 0
        failed = 0

        self.stdout.write(self.style.SUCCESS(f"Indexing {total} records..."))

        # Loop through all GroupIndividual objects
        for idx, obj in enumerate(queryset, 1):
            try:
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
                indexed += 1

                if idx % 1000 == 0:
                    self.stdout.write(
                        self.style.SUCCESS(f'Progress: {idx}/{total} ({indexed} indexed, {failed} failed)')
                    )

            except Exception as e:
                failed += 1
                if skip_errors:
                    self.stderr.write(
                        self.style.WARNING(f"Skipped {obj.id}: {str(e)}")
                    )
                else:
                    self.stdout.write(
                        self.style.ERROR(f"Failed at record {idx}/{total}: {str(e)}")
                    )
                    raise

        self.stdout.write(
            self.style.SUCCESS(f"Completed: {indexed} indexed, {failed} failed")
        )
