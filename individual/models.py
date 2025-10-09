import core
from django.conf import settings
from django.db import models, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _
from core.models import HistoryModel
from graphql import ResolveInfo
from location.models import Location, LocationManager


class Individual(HistoryModel):
    USE_CACHE = False
    first_name = models.CharField(max_length=255, null=False)
    last_name = models.CharField(max_length=255, null=False)
    dob = core.fields.DateField(null=False)
    #TODO WHY the HistoryModel json_ext was not enough
    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)

    location = models.ForeignKey(
        Location,
        models.DO_NOTHING,
        blank=True,
        null=True,
        related_name='individuals'
    )

    def __str__(self):
        return f'{self.first_name} {self.last_name}'

    class Meta:
        managed = True

    @classmethod
    def get_queryset(cls, queryset, user):
        if queryset is None:
            queryset = cls.objects.all()

        if not settings.ROW_SECURITY:
            return queryset

        if user.is_anonymous:
            return queryset.filter(id=-1)

        if not user.is_imis_admin:
            user_districts_match_individual = LocationManager().build_user_location_filter_query(
                user._u
            )
            individual_has_group = models.Q(("groupindividuals__group__isnull", False))
            user_districts_match_individual_group = LocationManager().build_user_location_filter_query(
                user._u,
                prefix='groupindividuals__group__location'
            )
            return queryset.filter(
                models.Q(
                    user_districts_match_individual
                    | (individual_has_group & user_districts_match_individual_group)
                )
            )

        return queryset

class IndividualDataSourceUpload(HistoryModel):
    USE_CACHE = False

    class Status(models.TextChoices):
        PENDING = 'PENDING', _('Pending')
        TRIGGERED = 'TRIGGERED', _('Triggered')
        IN_PROGRESS = 'IN_PROGRESS', _('In progress')
        SUCCESS = 'SUCCESS', _('Success')
        PARTIAL_SUCCESS = 'PARTIAL_SUCCESS', _('Partial Success')
        WAITING_FOR_VERIFICATION = 'WAITING_FOR_VERIFICATION', _('WAITING_FOR_VERIFICATION')
        FAIL = 'FAIL', _('Fail')

    source_name = models.CharField(max_length=255, null=False)
    source_type = models.CharField(max_length=255, null=False)

    status = models.CharField(max_length=255, choices=Status.choices, default=Status.PENDING)
    error = models.JSONField(blank=True, default=dict)


class IndividualDataSource(HistoryModel):
    USE_CACHE = False
    individual = models.ForeignKey(Individual, models.DO_NOTHING, blank=True, null=True)
    upload = models.ForeignKey(IndividualDataSourceUpload, models.DO_NOTHING, blank=True, null=True)
    validations = models.JSONField(blank=True, default=dict)
    # pmt_score = models.FloatField(editable=False, null=True)


class IndividualDataUploadRecords(HistoryModel):
    USE_CACHE = False
    data_upload = models.ForeignKey(IndividualDataSourceUpload, models.DO_NOTHING, null=False)
    workflow = models.CharField(max_length=50)

    def __str__(self):
        return f"Individual Import - {self.data_upload.source_name} {self.workflow} {self.date_created}"


class Group(HistoryModel):
    USE_CACHE = False
    code = models.CharField(max_length=64, blank=False, null=False)
    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)
    location = models.ForeignKey(
        Location,
        models.DO_NOTHING,
        blank=True,
        null=True,
        related_name='groups'
    )

    @classmethod
    def get_queryset(cls, queryset, user):
        if queryset is None:
            queryset = Group.objects.all()

        if not settings.ROW_SECURITY:
            return queryset

        if user.is_anonymous:
            return queryset.filter(id=-1)

        if not user.is_imis_admin:
            return queryset.filter(
                LocationManager().build_user_location_filter_query(
                    user._u
                )
            )
        return queryset

@receiver(post_save, sender=Group)
def update_member_individuals_location(sender, instance, **kwargs):
    with transaction.atomic():
        # has to save one-by-one instead of bulk update due to track history
        for individual in Individual.objects.filter(groupindividuals__group=instance):
            # only update individual location if group location is present,
            # because individuals import would create a group with empty locaiton which then takes on the location of the head
            if instance.location_id and individual.location_id != instance.location_id:
                individual.location_id=instance.location_id
                individual.save(user=instance.user_updated)

class GroupDataSource(HistoryModel):
    USE_CACHE = False
    group = models.ForeignKey(Group, models.DO_NOTHING, blank=True, null=True)
    upload = models.ForeignKey(IndividualDataSourceUpload, models.DO_NOTHING, blank=True, null=True)
    validations = models.JSONField(blank=True, default=dict)


class GroupIndividual(HistoryModel):
    USE_CACHE = False
    class Role(models.TextChoices):
        HEAD = 'HEAD', _('HEAD')
        SPOUSE = 'SPOUSE', _('SPOUSE')
        SON = 'SON', _('SON')
        DAUGHTER = 'DAUGHTER', _('DAUGHTER')
        GRANDFATHER = 'GRANDFATHER', _('GRANDFATHER')
        GRANDMOTHER = 'GRANDMOTHER', _('GRANDMOTHER')
        MOTHER = 'MOTHER', _('MOTHER')
        FATHER = 'FATHER', _('FATHER')
        GRANDSON = 'GRANDSON', _('GRANDSON')
        GRANDDAUGHTER = 'GRANDDAUGHTER', _('GRANDDAUGHTER')
        SISTER = 'SISTER', _('SISTER')
        BROTHER = 'BROTHER', _('BROTHER')
        OTHER_RELATIVE = 'OTHER RELATIVE', _('OTHER RELATIVE')
        NOT_RELATED = 'NOT RELATED', _('NOT RELATED')

    class RecipientType(models.TextChoices):
        PRIMARY = 'PRIMARY', _('PRIMARY')
        SECONDARY = 'SECONDARY', _('SECONDARY')

    group = models.ForeignKey(
        Group,
        models.DO_NOTHING,
        related_name='groupindividuals'
    )
    individual = models.ForeignKey(
        Individual,
        models.DO_NOTHING,
        related_name='groupindividuals'
    )
    role = models.CharField(max_length=255, choices=Role.choices, null=True, blank=True)
    recipient_type = models.CharField(max_length=255, choices=RecipientType.choices, null=True, blank=True)

    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)

    def save(self, *args, **kwargs):
        user = kwargs.get('user')
        if user:
            super().save(user=user)
        else:
            super().save(username=kwargs.get('username'))  
        from individual.services import GroupAndGroupIndividualAlignmentService
        service = GroupAndGroupIndividualAlignmentService(self.user_updated)
        service.handle_head_change(self.id, self.role, self.group_id)
        service.handle_primary_recipient_change(self.id, self.recipient_type, self.group_id)
        service.handle_assure_primary_recipient_in_group(self.group, self.recipient_type)
        service.ensure_location_consistent(self.group, self.individual, self.role)
        service.update_json_ext_for_group(self.group)

    def delete(self, *args, **kwargs):
        user = kwargs.get('user')
        if user:
            super().delete(user=user)
        else:
            super().delete(username=kwargs.get('username'))
        
        from individual.services import GroupAndGroupIndividualAlignmentService
        service = GroupAndGroupIndividualAlignmentService(self.user_updated)
        service.update_json_ext_for_group(self.group)

    @classmethod
    def get_queryset(cls, queryset, user):
        if queryset is None:
            queryset = GroupIndividual.objects.all()

        if not settings.ROW_SECURITY:
            return queryset

        if user.is_anonymous:
            return queryset.filter(id=-1)

        if not user.is_imis_admin:
            return queryset.filter(
                LocationManager().build_user_location_filter_query(
                    user._u, prefix='group__location'
                )
            )
        return queryset
