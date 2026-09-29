from django.db import migrations, models


class Migration(migrations.Migration):
    """Add the questionnaire relationships the role list could not express.

    Codes 4, 7, 10, 11, 12 and 13 all collapsed to OTHER RELATIVE, so a report
    could not tell a son-in-law from a step child. Choices are not enforced in
    the database, so this alters metadata only and rewrites no rows.
    """

    dependencies = [
        ('individual', '0030_groupindividual_is_active'),
    ]

    operations = [
        migrations.AlterField(
            model_name='groupindividual',
            name='role',
            field=models.CharField(blank=True, choices=[('HEAD', 'HEAD'), ('SPOUSE', 'SPOUSE'), ('SON', 'SON'), ('DAUGHTER', 'DAUGHTER'), ('GRANDFATHER', 'GRANDFATHER'), ('GRANDMOTHER', 'GRANDMOTHER'), ('MOTHER', 'MOTHER'), ('FATHER', 'FATHER'), ('GRANDSON', 'GRANDSON'), ('GRANDDAUGHTER', 'GRANDDAUGHTER'), ('SISTER', 'SISTER'), ('BROTHER', 'BROTHER'), ('SON IN LAW', 'SON IN LAW'), ('DAUGHTER IN LAW', 'DAUGHTER IN LAW'), ('FATHER IN LAW', 'FATHER IN LAW'), ('MOTHER IN LAW', 'MOTHER IN LAW'), ('STEP CHILD', 'STEP CHILD'), ('CO-WIFE', 'CO-WIFE'), ('BROTHER IN LAW', 'BROTHER IN LAW'), ('SISTER IN LAW', 'SISTER IN LAW'), ('HOUSE HELP', 'HOUSE HELP'), ('OTHER RELATIVE', 'OTHER RELATIVE'), ('NOT RELATED', 'NOT RELATED')], max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name='historicalgroupindividual',
            name='role',
            field=models.CharField(blank=True, choices=[('HEAD', 'HEAD'), ('SPOUSE', 'SPOUSE'), ('SON', 'SON'), ('DAUGHTER', 'DAUGHTER'), ('GRANDFATHER', 'GRANDFATHER'), ('GRANDMOTHER', 'GRANDMOTHER'), ('MOTHER', 'MOTHER'), ('FATHER', 'FATHER'), ('GRANDSON', 'GRANDSON'), ('GRANDDAUGHTER', 'GRANDDAUGHTER'), ('SISTER', 'SISTER'), ('BROTHER', 'BROTHER'), ('SON IN LAW', 'SON IN LAW'), ('DAUGHTER IN LAW', 'DAUGHTER IN LAW'), ('FATHER IN LAW', 'FATHER IN LAW'), ('MOTHER IN LAW', 'MOTHER IN LAW'), ('STEP CHILD', 'STEP CHILD'), ('CO-WIFE', 'CO-WIFE'), ('BROTHER IN LAW', 'BROTHER IN LAW'), ('SISTER IN LAW', 'SISTER IN LAW'), ('HOUSE HELP', 'HOUSE HELP'), ('OTHER RELATIVE', 'OTHER RELATIVE'), ('NOT RELATED', 'NOT RELATED')], max_length=255, null=True),
        ),
    ]
