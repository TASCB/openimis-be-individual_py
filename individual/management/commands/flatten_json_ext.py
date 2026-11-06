from django.core.management.base import BaseCommand
from django.db import connection, transaction

TABLES = {"individuals": "individual_individual", "groups": "individual_group"}
STALE_NESTED_KEYS = ("pmt_score", "pmt_class", "pmt_cutoff_used")

NESTED = """"Json_ext" ? 'json_ext' AND jsonb_typeof("Json_ext"->'json_ext') = 'object'"""

FLATTEN = f"""
WITH batch AS (
    SELECT "UUID" FROM {{table}} WHERE {NESTED} LIMIT %s FOR UPDATE SKIP LOCKED
)
UPDATE {{table}} t
SET "Json_ext" = ((t."Json_ext"->'json_ext') - %s::text[]) || (t."Json_ext" - 'json_ext')
FROM batch WHERE t."UUID" = batch."UUID"
"""

DROP_BROKEN = """
UPDATE {table} SET "Json_ext" = "Json_ext" - 'json_ext'
WHERE "Json_ext" ? 'json_ext' AND jsonb_typeof("Json_ext"->'json_ext') <> 'object'
"""


class Command(BaseCommand):
    help = "Move the nested Json_ext->'json_ext' keys of individuals and households to the top level."

    def add_arguments(self, parser):
        parser.add_argument("--table", choices=["individuals", "groups", "all"], default="all")
        parser.add_argument("--batch-size", type=int, default=10000)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        names = list(TABLES) if options["table"] == "all" else [options["table"]]
        self.stdout.write(f"Database: {connection.settings_dict['NAME']}")
        for name in names:
            table = TABLES[name]
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT count(*) FROM {table} WHERE {NESTED}")
                nested = cursor.fetchone()[0]
                cursor.execute(
                    f"""SELECT count(*) FROM {table} WHERE "Json_ext" ? 'json_ext'
                    AND jsonb_typeof("Json_ext"->'json_ext') <> 'object'"""
                )
                broken = cursor.fetchone()[0]
            self.stdout.write(f"{name}: {nested} nested, {broken} with a non-object json_ext")
            if options["dry_run"]:
                continue
            done = 0
            while True:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(FLATTEN.format(table=table), [options["batch_size"], list(STALE_NESTED_KEYS)])
                    count = cursor.rowcount
                if not count:
                    break
                done += count
                self.stdout.write(f"  {name}: {done}/{nested}")
            if broken:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute(DROP_BROKEN.format(table=table))
            self.stdout.write(self.style.SUCCESS(f"{name}: flattened {done}, removed {broken} non-object json_ext"))
        if not options["dry_run"]:
            self.stdout.write(f"Run VACUUM ANALYZE on {', '.join(TABLES[n] for n in names)} afterwards.")
