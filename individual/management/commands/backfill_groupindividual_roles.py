"""Recompute GroupIndividual.role from the source relationship code and sex (the previous
table had codes 5-7 one slot off and 8 unmapped).

Uses bulk_update: GroupIndividual.save() cascades into head change, primary-recipient
promotion and location alignment, none of which a label repair wants. No history rows
are written for the correction.
"""
import re
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction

from individual.models import GroupIndividual, Individual
from individual.relationship_roles import (
    GENDER_BY_ROLE,
    OTHER_RELATIVE,
    normalize_gender,
    role_for_relationship,
)
from individual.services import IndividualImportService

HEAD = GroupIndividual.Role.HEAD

# The ETL builds member external ids as f"P3-{location:09d}-{interview:08d}-{code}-{ordinal}".
# Matching that exact shape matters: when the group code is missing the adapter
# falls back to the bare Survey Solutions interview key ("17-49-55-77"), whose
# segments are not relationship codes.
EXTERNAL_ID_RE = re.compile(r"^[^-]+-\d{9}-\d{8}-(\d+)-\d+$")


def _relationship_code(json_ext):
    """
    Source relationship code for a member.

    Imports before 2026-08 did not persist rel_to_hhh, but the ETL encodes it in
    external_id as P3-<location>-<interview>-<code>-<ordinal>, so it stays
    recoverable there.
    """
    code = IndividualImportService._json_ext_lookup(
        json_ext,
        "rel_to_hhh",
        "individual_role_code",
        "relationship_to_head",
    )
    if code not in (None, ""):
        return str(code).strip()

    external_id = IndividualImportService._json_ext_lookup(json_ext, "external_id")
    if external_id:
        match = EXTERNAL_ID_RE.match(str(external_id))
        if match:
            return match.group(1)
    return None


class Command(BaseCommand):
    help = (
        "Recompute GroupIndividual.role from the source relationship code. "
        "Dry-run by default; pass --apply to write. "
        "Run with: python manage.py backfill_groupindividual_roles [--apply] "
        "[--since 2026-08-11] [--batch-size 1000] [--limit N] [--include-null-roles]"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the corrections. Without this the command only reports.",
        )
        parser.add_argument(
            "--since",
            default=None,
            help=(
                "Only rows created on or after this ISO date. Gendered roles were "
                "first written 2026-08-11; anything older is HEAD-or-null."
            ),
        )
        parser.add_argument("--batch-size", type=int, default=1000)
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument(
            "--include-null-roles",
            action="store_true",
            help=(
                "Also fill members whose role is null. Off by default: without a "
                "recoverable sex these resolve to OTHER RELATIVE, which is less "
                "informative than null."
            ),
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        batch_size = options["batch_size"]
        limit = options["limit"]
        include_null = options["include_null_roles"]
        since = options["since"]

        queryset = (
            GroupIndividual.objects.filter(is_deleted=False)
            .select_related("individual")
            .order_by("id")
        )
        if since:
            queryset = queryset.filter(date_created__gte=since)
        if not include_null:
            queryset = queryset.exclude(role__isnull=True)

        stats = Counter()
        transitions = Counter()
        pending_links = []
        pending_individuals = {}

        def flush():
            if not apply_changes:
                pending_links.clear()
                pending_individuals.clear()
                return
            with transaction.atomic():
                if pending_links:
                    GroupIndividual.objects.bulk_update(pending_links, ["role"])
                if pending_individuals:
                    Individual.objects.bulk_update(
                        list(pending_individuals.values()), ["json_ext"]
                    )
            pending_links.clear()
            pending_individuals.clear()

        for link in queryset.iterator(chunk_size=batch_size):
            if limit and stats["examined"] >= limit:
                break
            stats["examined"] += 1
            json_ext = link.individual.json_ext or {}

            code = _relationship_code(json_ext)
            if not code:
                stats["no_relationship_code"] += 1
                continue

            gender = normalize_gender(
                IndividualImportService._json_ext_lookup(json_ext, "gender")
            ) or GENDER_BY_ROLE.get(link.role)

            computed = role_for_relationship(code, gender)
            if not computed or computed == link.role:
                stats["already_correct"] += 1
                continue

            # Head membership is the one thing a label repair must not move: it
            # drives recipient selection and the group's mirrored json_ext.
            if link.role == HEAD or computed == HEAD:
                stats["head_skipped"] += 1
                continue

            if link.role is None and computed == OTHER_RELATIVE:
                stats["null_left_alone"] += 1
                continue

            transitions[(link.role, computed, code)] += 1
            stats["corrected"] += 1

            link.role = computed
            pending_links.append(link)

            stored_label = json_ext.get("individual_role")
            if stored_label and stored_label != computed:
                # Left stale, this label wins over the code on any future re-link.
                json_ext["individual_role"] = computed
                link.individual.json_ext = json_ext
                pending_individuals[link.individual_id] = link.individual
                stats["labels_rewritten"] += 1

            if len(pending_links) >= batch_size:
                flush()

        flush()

        self._report(stats, transitions, apply_changes)

    def _report(self, stats, transitions, apply_changes):
        mode = "APPLIED" if apply_changes else "DRY RUN (no changes written)"
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n{mode}"))

        if transitions:
            self.stdout.write("\n  code   from             -> to                 count")
            self.stdout.write("  " + "-" * 56)
            for (old, new, code), count in transitions.most_common():
                self.stdout.write(
                    f"  {code:<6} {str(old):<16} -> {new:<18} {count:>7}"
                )

        self.stdout.write("")
        for key in (
            "examined",
            "corrected",
            "labels_rewritten",
            "already_correct",
            "no_relationship_code",
            "head_skipped",
            "null_left_alone",
        ):
            self.stdout.write(f"  {key:<24} {stats[key]:>9}")

        if stats["corrected"] and not apply_changes:
            self.stdout.write(
                self.style.WARNING("\n  Re-run with --apply to write these corrections.")
            )
        elif apply_changes:
            self.stdout.write(
                self.style.SUCCESS(f"\n  Wrote {stats['corrected']} corrected roles.")
            )
