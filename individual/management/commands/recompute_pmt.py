import json
import uuid
from collections import Counter, defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

UPDATE_GROUPS = """
UPDATE individual_group g SET "Json_ext" = COALESCE(g."Json_ext", '{}'::jsonb) || v.patch
FROM unnest(%s::uuid[], %s::jsonb[]) AS v(id, patch) WHERE g."UUID" = v.id
"""

UPDATE_MEMBERS = """
UPDATE individual_individual i SET "Json_ext" = COALESCE(i."Json_ext", '{}'::jsonb) || v.patch
FROM individual_groupindividual gi, unnest(%s::uuid[], %s::jsonb[]) AS v(group_id, patch)
WHERE gi.group_id = v.group_id AND gi.individual_id = i."UUID" AND NOT gi."isDeleted"
"""


class Command(BaseCommand):
    help = "Recompute household PMT per district with bulk writes. Does not enrol into PCT."

    def add_arguments(self, parser):
        parser.add_argument("--district", action="append", default=[], help="District code; repeat for several")
        parser.add_argument("--all", action="store_true", help="Every district")
        parser.add_argument("--cutoff", type=float, help="Defaults to the active formula cutoff")
        parser.add_argument("--batch-size", type=int, default=2000)
        parser.add_argument("--username", default="Admin")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        from api_etl.workflows.pmt import compute_household_pmt_score, get_active_coeffs, survey_answers
        from core.models import User
        from location.models import Location

        if not options["district"] and not options["all"]:
            raise CommandError("Give --district CODE or --all.")
        user = User.objects.filter(username=options["username"], validity_to__isnull=True).first()
        if not user:
            raise CommandError(f"User {options['username']} not found.")

        coeffs = get_active_coeffs()
        cutoff = options["cutoff"] if options["cutoff"] is not None else float(coeffs["cutoff"])
        districts = options["district"] or list(
            Location.objects.filter(type="D", validity_to__isnull=True).order_by("code").values_list("code", flat=True)
        )
        self.stdout.write(f"Database: {connection.settings_dict['NAME']}  cutoff: {cutoff}  dry run: {options['dry_run']}")

        totals = Counter()
        for code in districts:
            result = self._district(code, coeffs, cutoff, user, options, compute_household_pmt_score, survey_answers)
            totals.update(result)
            self.stdout.write(f"{code}: {dict(result)}")
        self.stdout.write(self.style.SUCCESS(f"Total: {dict(totals)}"))

    def _district(self, code, coeffs, cutoff, user, options, compute, answers):
        from individual.models import Group, GroupIndividual, PmtRunProgress
        from individual.pmt_service import PmtService

        location_ids = PmtService._location_and_descendant_ids(code)
        group_ids = list(
            Group.objects.filter(is_deleted=False, location_id__in=location_ids).order_by("id").values_list("id", flat=True)
        )
        before = Counter(
            Group.objects.filter(id__in=group_ids).values_list("json_ext__pmt_class_household", flat=True)
        ) if group_ids else Counter()

        run_id = uuid.uuid4()
        now = timezone.now().isoformat()
        audit = {
            "pmt_cutoff_used": cutoff,
            "pmt_last_operation": "RERUN",
            "pmt_last_cutoff": cutoff,
            "pmt_last_updated_at": now,
            "pmt_last_mutation_id": str(run_id),
            "pmt_last_district_code": code,
        }
        result = Counter(households=len(group_ids))

        for start in range(0, len(group_ids), options["batch_size"]):
            batch = group_ids[start:start + options["batch_size"]]
            heads, members = {}, defaultdict(list)
            for group_id, role, json_ext, dob in GroupIndividual.objects.filter(
                group_id__in=batch, is_deleted=False, individual__is_deleted=False,
            ).values_list("group_id", "role", "individual__json_ext", "individual__dob"):
                members[group_id].append({"dob": dob})
                if role == GroupIndividual.Role.HEAD:
                    heads[group_id] = json_ext or {}

            ids, group_patches, member_patches = [], [], []
            for group_id in batch:
                if group_id not in heads:
                    result["no_head"] += 1
                    continue
                if not answers(heads[group_id]):
                    result["no_survey_answers"] += 1
                    continue
                score = compute({**heads[group_id], "household_size": len(members[group_id])}, members[group_id], coeffs)
                pmt_class = PmtService._classify_pmt(score, cutoff)
                result[pmt_class] += 1
                ids.append(str(group_id))
                group_patches.append(json.dumps(
                    {**audit, "pmt_score_household": score, "pmt_class_household": pmt_class}
                ))
                member_patches.append(json.dumps({**audit, "pmt_score": score, "pmt_class": pmt_class}))

            if ids and not options["dry_run"]:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(UPDATE_GROUPS, [ids, group_patches])
                    cursor.execute(UPDATE_MEMBERS, [ids, member_patches])
                    result["members_updated"] += cursor.rowcount

        if not options["dry_run"] and group_ids:
            PmtRunProgress.objects.create(
                mutation_id=run_id,
                status=PmtRunProgress.Status.COMPLETED,
                operation=PmtRunProgress.Operation.RERUN,
                district_code=code,
                user=user,
                pmt_cutoff=cutoff,
                total_groups=len(group_ids),
                processed_groups=result["POOR"] + result["NON_POOR"],
                processed_individuals=result["members_updated"],
                poor_groups_found=result["POOR"],
                poor_before=before.get("POOR", 0),
                non_poor_before=before.get("NON_POOR", 0),
                poor_after=result["POOR"],
                non_poor_after=result["NON_POOR"],
                completed_at=timezone.now(),
            )
        return result
