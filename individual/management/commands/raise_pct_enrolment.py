from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Raise one PCT enrolment approval task per district for POOR, consented households not yet in PCT."

    def add_arguments(self, parser):
        parser.add_argument("--district", action="append", default=[], help="District code; repeat for several")
        parser.add_argument("--all", action="store_true", help="Every district")
        parser.add_argument("--username", default="Admin")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        from core.models import User
        from individual.pct_enrolment import PctEnrolmentService
        from location.models import Location

        if not options["district"] and not options["all"]:
            raise CommandError("Give --district CODE or --all.")
        user = User.objects.filter(username=options["username"], validity_to__isnull=True).first()
        if not user:
            raise CommandError(f"User {options['username']} not found.")
        districts = options["district"] or list(
            Location.objects.filter(type="D", validity_to__isnull=True).order_by("code").values_list("code", flat=True)
        )
        self.stdout.write(f"Database: {connection.settings_dict['NAME']}  dry run: {options['dry_run']}")

        service = PctEnrolmentService(user)
        tasks = households = 0
        for code in districts:
            result = service.raise_for_district(code, dry_run=options["dry_run"]) or {}
            if result.get("detail") in ("pct_auto_enroll_disabled", "pct_benefit_plan_not_found"):
                raise CommandError(result["detail"])
            line = self._describe(result)
            if line:
                self.stdout.write(f"{code}: {line}")
                tasks += 1
                households += self._households(result)
        self.stdout.write(self.style.SUCCESS(f"Districts: {tasks}  households: {households}"))

    @staticmethod
    def _households(result):
        if result.get("data"):
            return result["data"]["json_ext"]["households"]
        return result.get("added") or (result.get("households", 0) if result.get("dry_run") else 0)

    @staticmethod
    def _describe(result):
        if result.get("dry_run") and result.get("households"):
            return f"{result['households']} new households"
        if result.get("added"):
            return f"{result['added']} added to open task {result['task_id']} (now {result['households']})"
        if result.get("data"):
            return f"new task {result['data'].get('id')} for {result['data']['json_ext']['households']} households"
        if result.get("existing"):
            return f"nothing new; open task {result['task_id']}"
        if "enrolments_created" in result:
            return f"enrolled directly: {result['enrolments_created']}"
        return None
