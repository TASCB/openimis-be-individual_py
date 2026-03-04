# Generated migration for PmtRunProgress model

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('individual', '0021_pmtenrollment_pmtconfig_historicalpmtenrollment_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='PmtRunProgress',
            fields=[
                ('mutation_id', models.UUIDField(help_text='Links to mutation log', primary_key=True, serialize=False)),
                ('status', models.CharField(choices=[('STARTED', 'Started'), ('CALCULATING', 'Calculating PMT Scores'), ('ENROLLING', 'Creating Enrollments'), ('COMPLETED', 'Completed'), ('FAILED', 'Failed')], default='STARTED', help_text='Current status of PMT rerun', max_length=20)),
                ('district_code', models.CharField(help_text='District code being processed', max_length=50)),
                ('total_groups', models.IntegerField(default=0, help_text='Total groups to process')),
                ('processed_groups', models.IntegerField(default=0, help_text='Groups processed so far')),
                ('total_individuals', models.IntegerField(default=0, help_text='Total individuals to process')),
                ('processed_individuals', models.IntegerField(default=0, help_text='Individuals processed so far')),
                ('poor_groups_found', models.IntegerField(default=0, help_text='POOR groups identified')),
                ('enrollments_created', models.IntegerField(default=0, help_text='PmtEnrollment records created')),
                ('errors', models.JSONField(default=list, help_text='List of errors encountered')),
                ('started_at', models.DateTimeField(auto_now_add=True, help_text='When rerun started')),
                ('completed_at', models.DateTimeField(blank=True, help_text='When rerun completed', null=True)),
            ],
            options={
                'verbose_name': 'PMT Run Progress',
                'verbose_name_plural': 'PMT Run Progress Records',
                'managed': True,
            },
        ),
    ]
