# Partial unique index on the active (group, individual) pair.
#
# Applied as RunSQL rather than Meta.constraints so upstream's models.py stays
# untouched, following 0025 and 0028.
#
# atomic=False: CREATE UNIQUE INDEX CONCURRENTLY cannot run inside a
# transaction. It fails if duplicate active pairs exist — resolve them first.

from django.db import migrations


class Migration(migrations.Migration):

    atomic = False

    dependencies = [
        ('individual', '0028_add_import_lookup_indexes'),
    ]

    operations = [
        migrations.RunSQL(
            sql="""
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
                individual_groupindividual_active_unique
            ON individual_groupindividual (group_id, individual_id)
            WHERE "isDeleted" = false;
            """,
            reverse_sql="""
            DROP INDEX CONCURRENTLY IF EXISTS individual_groupindividual_active_unique;
            """,
            state_operations=[],
        ),
    ]
