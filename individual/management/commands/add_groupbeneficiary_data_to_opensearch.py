from django.core.management.base import BaseCommand
from social_protection.models import GroupBeneficiary
from individual.documents import GroupBeneficiaryDocument


class Command(BaseCommand):
    help = (
        "Imports enrolled households (GroupBeneficiary) into OpenSearch. "
        "Run with: python manage.py add_groupbeneficiary_data_to_opensearch [--skip-errors] [--limit N]"
    )

    def add_arguments(self, parser):
        parser.add_argument('--skip-errors', action='store_true',
                            help='Skip records that fail indexing instead of crashing')
        parser.add_argument('--limit', type=int, default=None, help='Limit number of records to index')

    def handle(self, *args, **options):
        skip_errors = options.get('skip_errors', False)
        limit = options.get('limit')

        self.stdout.write(self.style.SUCCESS("Initializing index..."))
        GroupBeneficiaryDocument.init(index='group_beneficiary')

        queryset = (GroupBeneficiary.objects.filter(is_deleted=False)
                    .select_related('group', 'benefit_plan').order_by('date_created'))
        if limit:
            queryset = queryset[:limit]

        total = queryset.count()
        indexed = failed = 0
        document = GroupBeneficiaryDocument()
        self.stdout.write(self.style.SUCCESS(f"Indexing {total} records..."))

        for idx, obj in enumerate(queryset.iterator(chunk_size=2000), 1):
            try:
                GroupBeneficiaryDocument(meta={'id': obj.id}, **document.prepare(obj)).save()
                indexed += 1
                if idx % 1000 == 0:
                    self.stdout.write(self.style.SUCCESS(
                        f'Progress: {idx}/{total} ({indexed} indexed, {failed} failed)'))
            except Exception as e:
                failed += 1
                if skip_errors:
                    self.stderr.write(self.style.WARNING(f"Skipped {obj.id}: {e}"))
                else:
                    self.stdout.write(self.style.ERROR(f"Failed at record {idx}/{total}: {e}"))
                    raise

        self.stdout.write(self.style.SUCCESS(f"Completed: {indexed} indexed, {failed} failed"))
