from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = "Create the PCT programme if missing. --recode-from <code> renames an existing programme instead."

    def add_arguments(self, parser):
        parser.add_argument('--recode-from', help="Existing programme code to rename to the PCT code (keeps its beneficiaries)")
        parser.add_argument('--username', default='Admin')

    def handle(self, *args, **options):
        from core.models import User
        from individual.apps import IndividualConfig
        from social_protection.models import BenefitPlan

        code = (IndividualConfig.pct_benefit_plan_code or '').strip()
        name = IndividualConfig.pct_benefit_plan_name
        if not code:
            raise CommandError("pct_benefit_plan_code is not configured.")
        user = User.objects.filter(username=options['username'], validity_to__isnull=True).first()
        if not user:
            raise CommandError(f"User {options['username']} not found.")

        plans = BenefitPlan.objects.filter(is_deleted=False)
        existing = plans.filter(code=code).first()
        if existing:
            if existing.type != BenefitPlan.BenefitPlanType.GROUP_TYPE:
                raise CommandError(f"Programme {code} exists but is not a household (GROUP) programme.")
            self.stdout.write(self.style.SUCCESS(f"Programme {code} ({existing.name}) already exists."))
            return

        with transaction.atomic():
            source = options['recode_from']
            if source:
                plan = plans.filter(code=source).first()
                if not plan:
                    raise CommandError(f"Programme {source} not found.")
                if plan.type != BenefitPlan.BenefitPlanType.GROUP_TYPE:
                    raise CommandError(f"Programme {source} is not a household (GROUP) programme.")
                plan.code, plan.name = code, name
                plan.save(user=user)
                self.stdout.write(self.style.SUCCESS(f"Programme {source} renamed to {code} ({name})."))
                return
            BenefitPlan(
                code=code,
                name=name,
                type=BenefitPlan.BenefitPlanType.GROUP_TYPE,
                institution='TASAF',
                description='Households classified POOR by the PMT and enrolled through import approval',
                date_valid_from=timezone.now().date(),
            ).save(user=user)
            self.stdout.write(self.style.SUCCESS(f"Programme {code} ({name}) created."))
