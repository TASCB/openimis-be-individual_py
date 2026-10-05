from django.db import migrations

PMT_ROWS = """NOT "isDeleted" AND ("Json_ext" ->> 'pmt_class_household') IS NOT NULL"""


class Migration(migrations.Migration):

    dependencies = [
        ('individual', '0031_groupindividual_role_questionnaire_values'),
    ]

    operations = [
        migrations.RunSQL(
            f"""CREATE INDEX IF NOT EXISTS idx_group_pmt_location_updated
                ON individual_group (location_id, "DateUpdated", "UUID") WHERE {PMT_ROWS};""",
            "DROP INDEX IF EXISTS idx_group_pmt_location_updated;",
        ),
        migrations.RunSQL(
            f"""CREATE INDEX IF NOT EXISTS idx_group_pmt_updated
                ON individual_group ("DateUpdated", "UUID") WHERE {PMT_ROWS};""",
            "DROP INDEX IF EXISTS idx_group_pmt_updated;",
        ),
        migrations.RunSQL(
            "DROP INDEX IF EXISTS tmp_stat_pmt_class;",
            migrations.RunSQL.noop,
        ),
    ]
