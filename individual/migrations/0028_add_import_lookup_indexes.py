# Group-code lookup and import dedup indexes for the import path.
#
# The external_id expression must match the workflow SQL character for character
# (including the NULLIF) or the planner will not use the index.
#
# atomic=False: CREATE INDEX CONCURRENTLY cannot run inside a transaction. A
# failed build leaves an INVALID index — drop it and re-run.

from django.db import migrations


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('individual', '0027_add_pmt_formula_rights_to_admin'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_individual_group_code
            ON individual_group USING BTREE (code)
            WHERE "isDeleted" = false;
            """,
            reverse_sql="""
            DROP INDEX CONCURRENTLY IF EXISTS idx_individual_group_code;
            """,
            state_operations=[],
        ),
        migrations.RunSQL(
            sql="""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_individual_external_id_norm
            ON individual_individual USING BTREE
                ((lower(NULLIF(btrim("Json_ext"->>'external_id'), ''))))
            WHERE "isDeleted" = false;
            """,
            reverse_sql="""
            DROP INDEX CONCURRENTLY IF EXISTS idx_individual_external_id_norm;
            """,
            state_operations=[],
        ),
    ]
