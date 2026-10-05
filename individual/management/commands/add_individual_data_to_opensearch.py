from django.core.management.base import BaseCommand
from django.db.models import OuterRef, Subquery
from django.db.models.fields.json import KeyTextTransform
from individual.models import GroupIndividual, Individual
from individual.documents import IndividualDocument, extract_disability, extract_gender, region_and_district


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
        household_pmt = (GroupIndividual.objects
                         .filter(individual=OuterRef('pk'), is_deleted=False, is_active=True)
                         .order_by('-date_created')
                         .annotate(pmt=KeyTextTransform('pmt_class_household', 'group__json_ext'))
                         .values('pmt')[:1])
        queryset = Individual.objects.annotate(household_pmt=Subquery(household_pmt))
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
                    gender=extract_gender(obj.json_ext),
                    date_created=obj.date_created,
                    region=region_and_district(obj.location_id)[0],
                    district=region_and_district(obj.location_id)[1],
                    disability=extract_disability(obj.json_ext),
                    pmt_class=obj.household_pmt or (obj.json_ext or {}).get('pmt_class'),
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
