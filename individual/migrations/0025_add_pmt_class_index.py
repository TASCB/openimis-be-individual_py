# Partial index on individual_group("Json_ext"->>'pmt_class_household') filtered
# by "isDeleted" = false. Speeds up the PMT audit summary page (PmtService.
# get_pmt_audit_summary -> _base_groups_with_pmt), which otherwise sequentially
# scans the whole individual_group table on every request.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('individual', '0024_flatten_imported_json_ext_again'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE INDEX IF NOT EXISTS idx_individual_group_pmt_class
            ON individual_group USING BTREE (("Json_ext"->>'pmt_class_household'))
            WHERE "isDeleted" = false;
            """,
            reverse_sql="""
            DROP INDEX IF EXISTS idx_individual_group_pmt_class;
            """,
            state_operations=[],
        ),
    ]
