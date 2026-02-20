from django.core.management.base import BaseCommand
from individual.models import Individual
from individual.documents import IndividualDocument


class Command(BaseCommand):
    help = (
        "Imports Individual data into OpenSearch. "
        "Run with: python manage.py add_individual_data_to_opensearch [--skip-errors] [--limit N]"
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
        IndividualDocument.init(index='individual')

        # Get queryset
        queryset = Individual.objects.all()
        if limit:
            queryset = queryset[:limit]

        total = queryset.count()
        indexed = 0
        failed = 0

        self.stdout.write(self.style.SUCCESS(f"Indexing {total} records..."))

        # Loop through all Individual objects
        for idx, obj in enumerate(queryset, 1):
            try:
                doc = IndividualDocument(
                    meta={'id': obj.id},  # set document ID
                    first_name=obj.first_name,
                    last_name=obj.last_name,
                    dob=obj.dob,
                    date_created=obj.date_created,
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
