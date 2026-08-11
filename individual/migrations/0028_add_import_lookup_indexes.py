# The external_id expression must match the workflow SQL exactly, NULLIF
# included, or the planner will not use the index.

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
