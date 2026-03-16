# Generated migration to add index on json_ext consent_res field

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('individual', '0018_alter_groupindividual_role_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE INDEX IF NOT EXISTS idx_individual_consent_res
            ON individual_individual USING BTREE (("Json_ext"->>'consent_res'));
            """,
            reverse_sql="""
            DROP INDEX IF EXISTS idx_individual_consent_res;
            """,
            state_operations=[],
        ),
    ]
