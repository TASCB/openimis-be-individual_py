import threading
from contextlib import contextmanager

from django.conf import settings
from django.db import models, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

import core
from core.models import HistoryModel
from graphql import ResolveInfo
from location.models import Location, LocationManager


_group_aggregate_state = threading.local()


def group_aggregates_suppressed():
    return getattr(_group_aggregate_state, "suppressed", False)


@contextmanager
def suppress_group_aggregate_updates():
    """
    Skip the per-save group aggregate rebuild for the duration of this block.

    For bulk callers that rebuild once per group themselves afterwards; the
    rebuild is not optional, or group.json_ext is left stale. Thread-local and
    default-off, so interactive edits are unaffected.
    """
    previous = getattr(_group_aggregate_state, "suppressed", False)
    _group_aggregate_state.suppressed = True
    try:
        yield
    finally:
        _group_aggregate_state.suppressed = previous



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


class PmtConfig(HistoryModel):
    """
    PMT Configuration model for storing PMT parameters per location.
    Allows customization of PMT cutoff and calculation parameters by region/district.
    """
    USE_CACHE = False

    location = models.OneToOneField(
        Location,
        models.DO_NOTHING,
        related_name='pmt_config',
        help_text="Location (District or Region) this PMT config applies to"
    )

    pmt_cutoff = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=11.01,
        help_text="PMT score cutoff for POOR classification (0-50). Score <= cutoff = POOR"
    )

    is_active = models.BooleanField(
        default=True,
        help_text="Whether this PMT config is active for calculations"
    )

    json_ext = models.JSONField(
        db_column="Json_ext",
        blank=True,
        default=dict,
        help_text="Extended JSON data (e.g., regional parameters, poverty line thresholds)"
    )

    class Meta:
        managed = True
        verbose_name = "PMT Configuration"
        verbose_name_plural = "PMT Configurations"

    def __str__(self):
        return f"PMT Config - {self.location.name} (Cutoff: {self.pmt_cutoff})"

    @classmethod
    def get_queryset(cls, queryset, user):
        if queryset is None:
            queryset = cls.objects.all()

        if not settings.ROW_SECURITY:
            return queryset

        if user.is_anonymous:
            return queryset.filter(id=-1)

        if not user.is_imis_admin:
            return queryset.filter(
                LocationManager().build_user_location_filter_query(
                    user._u, prefix='location'
                )
            )
        return queryset


class PmtEnrollment(HistoryModel):
    """
    PMT Enrollment tracking model for managing group enrollment based on PMT status.
    Tracks when households are enrolled/disenrolled based on PMT changes.
    """
    USE_CACHE = False

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        ENROLLED = "ENROLLED", _("Enrolled in Program")
        DISENROLLED = "DISENROLLED", _("Disenrolled from Program")
        SUSPENDED = "SUSPENDED", _("Suspended")
        REJECTED = "REJECTED", _("Rejected")

    class PmtClass(models.TextChoices):
        POOR = "POOR", _("Poor")
        NON_POOR = "NON_POOR", _("Non-Poor")

    group = models.ForeignKey(
        Group,
        models.DO_NOTHING,
        related_name='pmt_enrollments',
        help_text="Household/Group being enrolled"
    )

    pmt_class = models.CharField(
        max_length=20,
        choices=PmtClass.choices,
        help_text="PMT classification at time of enrollment"
    )

    pmt_score = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        help_text="PMT score at time of enrollment"
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        help_text="Current enrollment status"
    )

    # Links to benefit plan enrollment if this triggered an enrollment
    beneficiary_id = models.IntegerField(
        blank=True,
        null=True,
        help_text="ID of beneficiary record (if auto-enrolled)"
    )

    enrollment_date = models.DateTimeField(
        auto_now_add=False,
        blank=True,
        null=True,
        help_text="Date when enrollment was processed"
    )

    disenrollment_date = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Date when disenrollment was processed"
    )

    disenrollment_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Reason for disenrollment (e.g., PMT status changed to NON_POOR)"
    )

    json_ext = models.JSONField(
        db_column="Json_ext",
        blank=True,
        default=dict,
        help_text="Extended data (e.g., trigger details, approval notes, decision maker info)"
    )

    class Meta:
        managed = True
        verbose_name = "PMT Enrollment"
        verbose_name_plural = "PMT Enrollments"
        ordering = ['-date_created']

    def __str__(self):
        return f"{self.group.code} - {self.get_pmt_class_display()} ({self.get_status_display()})"

    @classmethod
    def get_queryset(cls, queryset, user):
        if queryset is None:
            queryset = cls.objects.all()

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


class PmtRunProgress(models.Model):
    """
    Tracks progress of PMT rerun operations.
    Allows frontend to display percentage progress to user.
    """
    class Status(models.TextChoices):
        STARTED = 'STARTED', _('Started')
        CALCULATING = 'CALCULATING', _('Calculating PMT Scores')
        ENROLLING = 'ENROLLING', _('Creating Enrollments')
        COMPLETED = 'COMPLETED', _('Completed')
        FAILED = 'FAILED', _('Failed')

    mutation_id = models.UUIDField(primary_key=True, help_text="Links to mutation log")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.STARTED,
        help_text="Current status of PMT rerun"
    )
    district_code = models.CharField(max_length=50, help_text="District code being processed")

    # Progress tracking
    total_groups = models.IntegerField(default=0, help_text="Total groups to process")
    processed_groups = models.IntegerField(default=0, help_text="Groups processed so far")
    total_individuals = models.IntegerField(default=0, help_text="Total individuals to process")
    processed_individuals = models.IntegerField(default=0, help_text="Individuals processed so far")

    # Results
    poor_groups_found = models.IntegerField(default=0, help_text="POOR groups identified")
    enrollments_created = models.IntegerField(default=0, help_text="PmtEnrollment records created")
    errors = models.JSONField(default=list, help_text="List of errors encountered")

    # Timestamps
    started_at = models.DateTimeField(auto_now_add=True, help_text="When rerun started")
    completed_at = models.DateTimeField(null=True, blank=True, help_text="When rerun completed")

    class Meta:
        managed = True
        verbose_name = "PMT Run Progress"
        verbose_name_plural = "PMT Run Progress Records"

    def __str__(self):
        return f"PMT Run {self.mutation_id} - {self.get_status_display()}"

    @property
    def percentage_complete(self):
        """Calculate percentage complete based on processed groups."""
        if self.total_groups == 0:
            return 0
        return int((self.processed_groups / self.total_groups) * 100)

    @property
    def status_message(self):
        """Get human-readable status message."""
        if self.status == self.Status.STARTED:
            return "Initializing..."
        elif self.status == self.Status.CALCULATING:
            return f"Processing households: {self.percentage_complete}% ({self.processed_groups}/{self.total_groups})"
        elif self.status == self.Status.ENROLLING:
            return f"Enrolling poor households: {self.enrollments_created} created"
        elif self.status == self.Status.COMPLETED:
            return f"Completed: {self.enrollments_created} households enrolled"
        elif self.status == self.Status.FAILED:
            return f"Failed: {', '.join(self.errors[:2])}"  # Show first 2 errors
        return "Processing..."


# ---------------------------------------------------------------------------
# Global PMT formula configuration (maker-checker editable)
# ---------------------------------------------------------------------------
class PmtGlobalFormula(HistoryModel):
    """
    Single, system-wide PMT scoring formula. Holds the regression coefficients
    and the POOR/NON_POOR cutoff that were previously hard-coded in
    ``api_etl.workflows.pmt``.

    Exactly one active row is expected (``get_active``). Edits go through the
    tasks_management maker-checker flow (see ``PmtGlobalFormulaService``), so a
    proposed change only becomes effective once a second user approves it.

    The whole formula is stored in a single JSON column (``formula``) rather than
    typed float columns on purpose: core's ``pre_save`` validator resets any
    *falsy* field value back to its default, which would silently revert a
    coefficient legitimately edited to ``0``. Nested JSON values are not touched.

        formula = {
            "cutoff": 11.01,                # POOR if score <= cutoff
            "intercept": 11.688,
            "household_size_coef": -0.10,
            "working_age_coef": -0.043,     # per member aged 15-64
            "urban_coef": 0.0,
            "assets": {"9": 0.25, "5": 0.179, ...},  # asset code -> coefficient
        }
    """
    USE_CACHE = False

    is_active = models.BooleanField(
        default=True,
        help_text="Only the active formula is used for scoring/classification.",
    )

    formula = models.JSONField(
        blank=True,
        default=dict,
        help_text="Coefficients + cutoff. See model docstring for shape.",
    )

    json_ext = models.JSONField(db_column="Json_ext", blank=True, default=dict)

    class Meta:
        managed = True
        verbose_name = "PMT Global Formula"
        verbose_name_plural = "PMT Global Formula"

    def __str__(self):
        cutoff = (self.formula or {}).get("cutoff")
        return f"PMT Formula (cutoff {cutoff}, {'active' if self.is_active else 'inactive'})"

    @classmethod
    def get_active(cls):
        """Return the active formula row, or None when none is configured."""
        return cls.objects.filter(is_active=True, is_deleted=False).order_by("-date_updated").first()

    def as_coeffs(self) -> dict:
        """
        Merge the stored formula over the engine defaults and normalize, so the
        result is always a complete coefficient dict for compute_household_pmt_score.
        """
        # Lazy import keeps api_etl an optional dependency and avoids load-order cycles.
        try:
            from api_etl.workflows.pmt import DEFAULT_COEFFS
            coeffs = dict(DEFAULT_COEFFS)
        except Exception:
            coeffs = {}
        stored = self.formula if isinstance(self.formula, dict) else {}
        for key in ("cutoff", "intercept", "household_size_coef", "working_age_coef", "urban_coef"):
            if stored.get(key) is not None:
                try:
                    coeffs[key] = float(stored[key])
                except (TypeError, ValueError):
                    pass
        assets = {}
        for code, coef in (stored.get("assets") or coeffs.get("assets") or {}).items():
            try:
                assets[str(code)] = float(coef)
            except (TypeError, ValueError):
                continue
        coeffs["assets"] = assets
        return coeffs

    @classmethod
    def get_queryset(cls, queryset, user):
        if queryset is None:
            queryset = cls.objects.all()
        if not settings.ROW_SECURITY:
            return queryset
        if user.is_anonymous:
            return queryset.filter(id=-1)
        if not user.is_imis_admin:
            return queryset.filter(id=-1)
        return queryset
