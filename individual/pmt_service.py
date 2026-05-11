"""
PMT Service

Reuses PMT calculation from api_etl module and provides business logic
specific to the individual module (database operations, transactions, permissions).

This service handles:
- Querying households with PMT data
- Recalculating PMT scores with new cutoffs
- Persisting PMT results to Individual and Group models
- Managing PMT Configuration (CRUD operations)
- Managing PMT Enrollment with auto-enrollment workflow
"""

import logging
import time
from datetime import datetime
from django.core.exceptions import ValidationError
from django.db import transaction, IntegrityError
from django.db.models import Case, CharField, Count, F, IntegerField, Max, Prefetch, Q, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone

from core.services import BaseService
from individual.models import Individual, Group, GroupIndividual, PmtConfig, PmtEnrollment
from location.models import LocationManager

logger = logging.getLogger(__name__)


class PmtService(BaseService):
    """
    Service for managing PMT (Poverty Management Tool) operations.
    Reuses PMT algorithm from api_etl module.
    Handles database operations and row-level security.
    """

    def __init__(self, user):
        super().__init__(user)
        self._init_pmt_functions()

    def _init_pmt_functions(self):
        """
        Initialize PMT functions from api_etl.
        Gracefully handles if api_etl is not available.

        Note: We import compute_household_pmt_score (reusable algorithm),
        but NOT classify_pmt, because api_etl's classify_pmt uses global config cutoff,
        while our rerun feature needs dynamic cutoff as parameter.
        So we implement _classify_pmt locally.
        """
        try:
            from api_etl.workflows.pmt import compute_household_pmt_score
            self.compute_household_pmt_score = compute_household_pmt_score
            self.pmt_available = True
        except ImportError as e:
            logger.warning(f"api_etl module not available for PMT calculation: {e}")
            self.pmt_available = False

    @staticmethod
    def _classify_pmt(score, cutoff):
        """
        Classify household as POOR or NON_POOR based on PMT score and cutoff.

        This is NOT imported from api_etl because api_etl's classify_pmt uses
        the configured global cutoff, while PMT rerun needs a dynamic cutoff.

        Args:
            score: float PMT score
            cutoff: float Cutoff threshold

        Returns:
            str: "POOR" if score <= cutoff, "NON_POOR" otherwise, or None if invalid
        """
        if score is None or cutoff is None:
            return None
        try:
            s = float(score)
            c = float(cutoff)
            return "POOR" if s <= c else "NON_POOR"
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _apply_location_filter(queryset, district_code=None, region_code=None):
        if district_code:
            return queryset.filter(
                Q(location__code=district_code)
                | Q(location__parent__code=district_code)
                | Q(location__parent__parent__code=district_code)
            )

        if region_code:
            return queryset.filter(
                Q(location__code=region_code)
                | Q(location__parent__code=region_code)
                | Q(location__parent__parent__code=region_code)
                | Q(location__parent__parent__parent__code=region_code)
            )

        return queryset

    def get_households_with_pmt(self, district_code=None, region_code=None, offset=0, limit=10,
                                 search_text=None, pmt_class=None, pmt_cutoff=None):
        """
        Fetch households (groups) with PMT data for a district/region.

        Args:
            district_code: District code to filter by
            region_code: Region code to filter by
            offset: Pagination offset
            limit: Page size
            search_text: Search by group code or head name
            pmt_class: Filter by "POOR" or "NON_POOR"
            pmt_cutoff: PMT cutoff used for classification (informational, stored in group.json_ext)

        Returns:
            dict with households list and metadata (total_count, has_next, has_previous)
        """
        # Start with groups, applying row-level security
        queryset = Group.get_queryset(Group.objects.filter(is_deleted=False), self.user)

        # REQUIRED: Only include groups with PMT data
        queryset = queryset.filter(json_ext__pmt_class_household__isnull=False)

        queryset = self._apply_location_filter(
            queryset,
            district_code=district_code,
            region_code=region_code,
        )

        # Filter by PMT class if provided
        if pmt_class in ["POOR", "NON_POOR"]:
            queryset = queryset.filter(json_ext__pmt_class_household=pmt_class)

        # Search by group code or head name
        if search_text:
            queryset = queryset.filter(
                Q(code__icontains=search_text) |
                Q(json_ext__head__icontains=search_text)
            )

        # Get total count before pagination
        total_count = queryset.count()

        # Paginate
        group_individuals_prefetch = Prefetch(
            "groupindividuals",
            queryset=GroupIndividual.objects.filter(
                is_deleted=False,
                individual__is_deleted=False,
            ).select_related("individual"),
        )
        groups = list(
            queryset.select_related("location")
            .prefetch_related(group_individuals_prefetch)
            .order_by('-date_updated')[offset:offset + limit]
        )

        # Build result with household data
        households = []
        for group in groups:
            group_json_ext = group.json_ext or {}
            members = []
            head = None
            hhrep_code = None
            for group_individual in group.groupindividuals.all():
                member = group_individual.individual
                members.append(member)

                if head is None and group_individual.role == GroupIndividual.Role.HEAD:
                    head = member

                if hhrep_code is None:
                    hhrep_code = str((member.json_ext or {}).get("hhrep") or "").strip() or None

            representative = next(
                (
                    member for member in members
                    if str((member.json_ext or {}).get("individual_role_code") or "").strip() == hhrep_code
                ),
                None
            ) if hhrep_code else None
            member_count = len(members)

            household_data = {
                "group_uuid": str(group.uuid),
                "group_code": group.code,
                "hh_rep": f"{representative.first_name} {representative.last_name}" if representative else None,
                "head_uuid": str(head.uuid) if head else None,
                "head_name": f"{head.first_name} {head.last_name}" if head else None,
                "pmt_score": group_json_ext.get("pmt_score_household"),
                "pmt_class": group_json_ext.get("pmt_class_household"),
                "number_of_members": member_count,
                "location_code": group.location.code if group.location else None,
                "location_name": group.location.name if group.location else None,
            }
            households.append(household_data)

        return {
            "households": households,
            "total_count": total_count,
            "has_next": (offset + limit) < total_count,
            "has_previous": offset > 0,
            "offset": offset,
            "limit": limit,
        }

    def _base_groups_with_pmt(self, district_code=None, region_code=None):
        queryset = Group.get_queryset(
            Group.objects.filter(
                is_deleted=False,
                json_ext__pmt_class_household__isnull=False,
            ),
            self.user,
        )
        return self._apply_location_filter(
            queryset,
            district_code=district_code,
            region_code=region_code,
        )

    @staticmethod
    def _annotate_district_fields(queryset):
        return queryset.annotate(
            district_id=Case(
                When(location__type='D', then=F('location_id')),
                When(location__parent__type='D', then=F('location__parent_id')),
                When(location__parent__parent__type='D', then=F('location__parent__parent_id')),
                default=Value(None),
                output_field=IntegerField(),
            ),
            district_code=Case(
                When(location__type='D', then=F('location__code')),
                When(location__parent__type='D', then=F('location__parent__code')),
                When(location__parent__parent__type='D', then=F('location__parent__parent__code')),
                default=Value(None),
                output_field=CharField(),
            ),
            district_name=Case(
                When(location__type='D', then=F('location__name')),
                When(location__parent__type='D', then=F('location__parent__name')),
                When(location__parent__parent__type='D', then=F('location__parent__parent__name')),
                default=Value(None),
                output_field=CharField(),
            ),
        )

    # ================================
    # - Keep pagination + return shape unchanged
    # ================================

    def get_pmt_audit_summary(self, district_code=None, region_code=None, offset=0, limit=10):
        """
        Audit summary - returns ONLY districts that have been rerun with PMT data.
        Shows most recently rerun districts first.

        Key Filter: Only returns districts with at least one group containing PMT data
        (i.e., where json_ext__pmt_class_household is set to POOR or NON_POOR)

        Args:
            district_code: If provided, returns only that specific district (if it has PMT data)
            region_code: If provided, filters districts to that region
            offset: Pagination offset
            limit: Pagination limit

        Returns:
            dict with districts list (sorted by recency), pagination info, and total count
        """
        try:
            from django.core.cache import cache

            # (5) Short per-user TTL cache. The audit summary doesn't need to be
            # live - reruns dispatch async via Celery and the next refresh after
            # this TTL expires picks up new counts.
            user_id = getattr(self.user, "id", None) or "anon"
            cache_key = (
                f"pmt_audit_summary:{user_id}:"
                f"{district_code or ''}:{region_code or ''}:{offset}:{limit}"
            )
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

            base_queryset = self._annotate_district_fields(
                self._base_groups_with_pmt(district_code=district_code, region_code=region_code)
            ).exclude(district_id__isnull=True)

            summary_queryset = (
                base_queryset
                .values("district_id", "district_code", "district_name")
                .annotate(
                    latest_update=Max("date_updated"),
                    poor_count=Count("id", filter=Q(json_ext__pmt_class_household="POOR")),
                    non_poor_count=Count("id", filter=Q(json_ext__pmt_class_household="NON_POOR")),
                )
                .order_by("-latest_update")
            )

            # (4) Cheaper total: count distinct district_ids straight off the
            # filtered base queryset instead of wrapping the full grouped
            # aggregate in a subquery COUNT(*).
            total_count = base_queryset.values("district_id").distinct().count()
            rows = list(summary_queryset[offset:offset + limit])
            district_ids = [row["district_id"] for row in rows if row.get("district_id") is not None]

            latest_cutoff_by_district = {}
            if district_ids:
                # (3) Postgres DISTINCT ON (district_id) returns one row per
                # district (the most recently updated one), instead of every
                # group in the page's districts.
                cutoff_rows = (
                    base_queryset
                    .filter(district_id__in=district_ids)
                    .exclude(json_ext__pmt_cutoff_used__isnull=True)
                    .annotate(pmt_cutoff_text=KeyTextTransform("pmt_cutoff_used", "json_ext"))
                    .order_by("district_id", "-date_updated")
                    .distinct("district_id")
                    .values("district_id", "pmt_cutoff_text")
                )

                for cutoff_row in cutoff_rows:
                    latest_cutoff_by_district[cutoff_row["district_id"]] = (
                        cutoff_row["pmt_cutoff_text"]
                    )

            districts_data = []
            for row in rows:
                pmt_cutoff_used = latest_cutoff_by_district.get(row.get("district_id"))
                if pmt_cutoff_used not in (None, ""):
                    try:
                        pmt_cutoff_used = float(pmt_cutoff_used)
                    except (TypeError, ValueError):
                        pass

                districts_data.append({
                    "district_code": row.get("district_code"),
                    "district_name": row.get("district_name"),
                    "pmt_cutoff": pmt_cutoff_used,
                    "poor_count": row.get("poor_count", 0) or 0,
                    "non_poor_count": row.get("non_poor_count", 0) or 0,
                })

            result = {
                "districts": districts_data,
                "total_count": total_count,
                "has_next": (offset + limit) < total_count,
                "has_previous": offset > 0,
                "offset": offset,
                "limit": limit,
            }
            cache.set(cache_key, result, 60)
            return result

        except Exception as e:
            logger.error(f"Error in get_pmt_audit_summary: {str(e)}", exc_info=True)
            return {
                "districts": [],
                "total_count": 0,
                "has_next": False,
                "has_previous": False,
                "offset": offset,
                "limit": limit,
            }

    @transaction.atomic()
    def rerun_pmt(self, district_code, region_code=None, pmt_cutoff=11.01, mutation_id=None):
        """
        Rerun PMT for all households in a district/region with a new cutoff.
        Updates both Individual and Group json_ext with new PMT scores and classifications.

        Uses PMT calculation from api_etl.workflows.pmt module (reuse principle).
        Applies row-level security and handles transactions atomically.

        Args:
            district_code: Required - District code
            region_code: Optional - Region code (for validation)
            pmt_cutoff: New PMT cutoff threshold (default 11.01)
            mutation_id: Optional - Mutation ID for progress tracking

        Returns:
            dict with success status, updated counts, and any errors
        """
        start_time = time.time()
        logger.info(
            f"PMT rerun started: district={district_code}, "
            f"region={region_code}, cutoff={pmt_cutoff}, mutation_id={mutation_id}"
        )

        # Initialize progress tracking if mutation_id provided
        progress = None
        if mutation_id:
            try:
                from individual.models import PmtRunProgress
                progress, created = PmtRunProgress.objects.update_or_create(
                    mutation_id=mutation_id,
                    defaults={
                        'status': PmtRunProgress.Status.STARTED,
                        'district_code': district_code,
                    }
                )
                logger.info(f"PMT progress tracking initialized for mutation {mutation_id}")
            except Exception as e:
                logger.warning(f"Failed to initialize progress tracking: {str(e)}")

        # Check if PMT functions are available
        if not self.pmt_available:
            return {
                "success": False,
                "errors": ["PMT module (api_etl) is not available"],
                "updated_individuals": 0,
                "updated_groups": 0,
            }

        try:
            # Validate input
            if not district_code:
                return {
                    "success": False,
                    "errors": ["district_code is required"],
                    "updated_individuals": 0,
                    "updated_groups": 0,
                }

            # Convert cutoff to float
            try:
                pmt_cutoff = float(pmt_cutoff)
            except (TypeError, ValueError):
                return {
                    "success": False,
                    "errors": ["Invalid pmt_cutoff value - must be a valid number"],
                    "updated_individuals": 0,
                    "updated_groups": 0,
                }

            # Validate cutoff is in reasonable range (0-50)
            if pmt_cutoff < 0 or pmt_cutoff > 50:
                return {
                    "success": False,
                    "errors": [f"PMT cutoff must be between 0 and 50, received {pmt_cutoff}"],
                    "updated_individuals": 0,
                    "updated_groups": 0,
                }

            # Fetch all groups (households) in district + child locations (wards, villages)
            # Searches 3 levels deep: district → ward → village
            member_prefetch = Prefetch(
                "groupindividuals",
                queryset=GroupIndividual.objects.filter(
                    is_deleted=False,
                    individual__is_deleted=False,
                ).select_related("individual"),
            )
            groups_query = self._apply_location_filter(
                Group.objects.filter(is_deleted=False),
                district_code=district_code,
                region_code=region_code,
            ).prefetch_related(member_prefetch)

            groups = Group.get_queryset(groups_query, self.user)

            # Convert to list to count and iterate
            groups_list = list(groups)
            total_groups = len(groups_list)
            total_individuals = sum(len(group.groupindividuals.all()) for group in groups_list)

            # Update progress: set total count and status to CALCULATING
            if progress:
                progress.total_groups = total_groups
                progress.total_individuals = total_individuals
                progress.status = PmtRunProgress.Status.CALCULATING
                progress.save()

            updated_individuals_count = 0
            updated_groups_count = 0
            errors = []

            for idx, group in enumerate(groups_list):
                try:
                    group_individuals = list(group.groupindividuals.all())
                    members = [group_individual.individual for group_individual in group_individuals]
                    head = next(
                        (
                            group_individual.individual for group_individual in group_individuals
                            if group_individual.role == GroupIndividual.Role.HEAD
                        ),
                        None,
                    )

                    if not head:
                        continue

                    # Prepare household data from HEAD's json_ext
                    head_json_ext = head.json_ext or {}
                    head.json_ext = head_json_ext
                    raw_data = head_json_ext.get("raw") or {}

                    # Extract fields with validation
                    assets = head_json_ext.get("assets_owned") or raw_data.get("assets_owned")
                    settlement = head_json_ext.get("settlement_type") or raw_data.get("settlement_type")

                    # Log warnings for missing data
                    if not assets:
                        logger.warning(
                            f"Missing assets_owned for group {group.code} - "
                            f"household may be underestimated in poverty assessment"
                        )
                        assets = []  # Empty list means 0 indicators for all asset coefficients

                    if not settlement:
                        logger.warning(
                            f"Missing settlement_type for group {group.code} - "
                            f"defaulting to rural for PMT calculation"
                        )
                        settlement = "rural"

                    hh_data = {
                        "household_size": len(members),
                        "assets_owned": assets,
                        "settlement_type": settlement,
                    }

                    # Prepare members list with dob
                    members_data = [{"dob": m.dob} for m in members]

                    # REUSE: Call PMT score calculation from api_etl module
                    new_pmt_score = self.compute_household_pmt_score(hh_data, members_data)

                    # Sanity check: PMT scores should typically be between 5-15
                    # (Outside this range might indicate data quality issues)
                    if new_pmt_score < 5 or new_pmt_score > 15:
                        logger.warning(
                            f"Unusual PMT score for group {group.code}: {new_pmt_score} "
                            f"(typical range 5-15). Verify household data quality."
                        )

                    # LOCAL: Classify with new cutoff (not in api_etl since it uses global config)
                    new_pmt_class = self._classify_pmt(new_pmt_score, pmt_cutoff)

                    # Update HEAD individual with new PMT
                    head.json_ext["pmt_score"] = new_pmt_score
                    if new_pmt_class:
                        head.json_ext["pmt_class"] = new_pmt_class
                    try:
                        head.save(user=self.user)
                    except ValidationError as ve:
                        if 'no changes in fields' not in str(ve):
                            raise
                    updated_individuals_count += 1

                    # Update all other household members with same PMT
                    for member in members:
                        if member.id == head.id:
                            continue
                        member.json_ext = member.json_ext or {}
                        member.json_ext["pmt_score"] = new_pmt_score
                        if new_pmt_class:
                            member.json_ext["pmt_class"] = new_pmt_class
                        try:
                            member.save(user=self.user)
                        except ValidationError as ve:
                            if 'no changes in fields' not in str(ve):
                                raise
                        updated_individuals_count += 1

                    # Update group with household-level PMT (mirrors HEAD's PMT)
                    group.json_ext = group.json_ext or {}
                    group.json_ext["pmt_score_household"] = new_pmt_score
                    if new_pmt_class:
                        group.json_ext["pmt_class_household"] = new_pmt_class

                    # Store the cutoff actually used for this rerun (so audit summary can display it)
                    group.json_ext["pmt_cutoff_used"] = float(pmt_cutoff)

                    try:
                        group.save(user=self.user)
                    except ValidationError as ve:
                        if 'no changes in fields' not in str(ve):
                            raise
                    updated_groups_count += 1

                    # Update progress every 10 groups to avoid excessive database writes
                    if progress and (updated_groups_count % 10 == 0 or updated_groups_count == total_groups):
                        progress.processed_groups = updated_groups_count
                        progress.processed_individuals = updated_individuals_count
                        progress.save()

                except (IntegrityError, ValueError, TypeError) as e:
                    # Log full error internally, but don't expose technical details to frontend
                    logger.error(
                        f"Error updating PMT for group {group.code}: {str(e)}",
                        exc_info=True
                    )
                    errors.append(f"Failed to update household {group.code} - verify data quality")
                except Exception as e:
                    # Unexpected error - don't continue processing
                    logger.error(
                        f"Unexpected error in PMT rerun for group {group.code}: {str(e)}",
                        exc_info=True,
                        extra={"group_code": group.code}
                    )
                    errors.append("An unexpected error occurred - the operation may be incomplete")
                    # Break here to avoid cascading failures
                    break

            # Auto-enrollment workflow: Create PmtEnrollment records for POOR households
            if updated_groups_count > 0:
                logger.info(f"Triggering auto-enrollment for {updated_groups_count} groups")

                # Update progress status to ENROLLING
                if progress:
                    progress.status = PmtRunProgress.Status.ENROLLING
                    progress.save()

                try:
                    enrollments_count = self._auto_enroll_poor_households(
                        district_code=district_code,
                        pmt_cutoff=pmt_cutoff,
                        progress=progress
                    )
                    logger.info("Auto-enrollment workflow completed successfully")

                    # Update progress with enrollment count
                    if progress:
                        progress.enrollments_created = enrollments_count
                        progress.poor_groups_found = enrollments_count
                        progress.status = PmtRunProgress.Status.COMPLETED
                        progress.completed_at = timezone.now()
                        progress.save()

                except Exception as e:
                    logger.error(f"Auto-enrollment workflow failed: {str(e)}", exc_info=True)
                    # Don't fail the whole mutation if auto-enrollment fails
                    errors.append(f"Auto-enrollment warning: {str(e)}")

                    # Mark as completed with error
                    if progress:
                        progress.status = PmtRunProgress.Status.COMPLETED
                        progress.completed_at = timezone.now()
                        progress.save()

            elapsed = time.time() - start_time
            logger.info(
                f"PMT rerun completed: {updated_groups_count} groups, "
                f"{updated_individuals_count} individuals processed in {elapsed:.2f}s"
            )

            return {
                "success": len(errors) == 0,
                "errors": errors,
                "updated_individuals": updated_individuals_count,
                "updated_groups": updated_groups_count,
                "district_code": district_code,
            }

        except Exception as e:
            logger.error(f"Unexpected error in rerun_pmt: {str(e)}", exc_info=True)
            return {
                "success": False,
                "errors": ["An unexpected error occurred - please contact administrator"],
                "updated_individuals": 0,
                "updated_groups": 0,
            }

    def _auto_enroll_poor_households(self, district_code, pmt_cutoff, progress=None):
        """
        Auto-enrollment workflow after PMT rerun.
        Creates PmtEnrollment records for all POOR households in the district.

        Args:
            district_code: District code
            pmt_cutoff: PMT cutoff threshold used for classification
            progress: Optional PmtRunProgress object to update with enrollment count

        Returns:
            int: Number of enrollments created
        """
        try:
            logger.info(f"Starting auto-enrollment workflow for district {district_code}")

            # Find all groups with POOR classification (including child locations)
            poor_groups = self._apply_location_filter(
                Group.objects.filter(
                    is_deleted=False,
                    json_ext__pmt_class_household="POOR",
                ),
                district_code=district_code,
            )

            poor_groups_list = list(poor_groups)
            logger.info(f"Found {len(poor_groups_list)} POOR groups in district {district_code}")

            existing_enrollment_group_ids = set(
                PmtEnrollment.objects.filter(
                    group_id__in=[group.id for group in poor_groups_list],
                    is_deleted=False,
                    status__in=[PmtEnrollment.Status.PENDING, PmtEnrollment.Status.ENROLLED],
                ).values_list("group_id", flat=True)
            )

            enrolled_count = 0
            for group in poor_groups_list:
                try:
                    if group.id in existing_enrollment_group_ids:
                        logger.debug(f"Group {group.code} already has active enrollment record")
                        continue

                    # Get PMT data
                    pmt_score = group.json_ext.get("pmt_score_household")
                    pmt_class = group.json_ext.get("pmt_class_household", "POOR")

                    if not pmt_score:
                        logger.warning(f"Missing PMT score for group {group.code}, skipping")
                        continue

                    # Create enrollment record
                    enrollment = PmtEnrollment(
                        group=group,
                        pmt_class=pmt_class,
                        pmt_score=pmt_score,
                        status=PmtEnrollment.Status.PENDING,
                        enrollment_date=timezone.now(),
                        json_ext={
                            "trigger": "auto_enrollment_after_rerun",
                            "cutoff_used": pmt_cutoff,
                            "created_by": str(self.user.id) if self.user else None,
                        }
                    )
                    enrollment.save(user=self.user)
                    enrolled_count += 1
                    logger.debug(f"Created enrollment for group {group.code}")

                except Exception as e:
                    logger.error(
                        f"Error auto-enrolling group {group.code}: {str(e)}",
                        exc_info=True
                    )
                    continue

            logger.info(f"Auto-enrollment completed: {enrolled_count} households enrolled")
            return enrolled_count

        except Exception as e:
            logger.error(f"Error in auto-enrollment workflow: {str(e)}", exc_info=True)
            raise  # Re-raise so caller knows about the error


class PmtConfigService(BaseService):
    """
    Service for managing PMT Configuration (CRUD operations).
    """

    def __init__(self, user):
        super().__init__(user)

    def create(self, data):
        """
        Create a new PMT Configuration.

        Args:
            data: dict with location_id, pmt_cutoff, is_active, json_ext

        Returns:
            dict with success status and error messages
        """
        try:
            from location.models import Location

            location_id = data.get('location_id')
            pmt_cutoff = data.get('pmt_cutoff', 11.01)
            is_active = data.get('is_active', True)
            json_ext = data.get('json_ext', {})

            if not location_id:
                return {"success": False, "error": "location_id is required"}

            # Verify location exists
            try:
                location = Location.objects.get(id=location_id)
            except Location.DoesNotExist:
                return {"success": False, "error": "Location not found"}

            # Check if config already exists for this location
            existing = PmtConfig.objects.filter(location=location, is_deleted=False).first()
            if existing:
                return {"success": False, "error": f"PMT Config already exists for {location.name}"}

            config = PmtConfig(
                location=location,
                pmt_cutoff=pmt_cutoff,
                is_active=is_active,
                json_ext=json_ext,
                user_updated=self.user
            )
            config.save(user=self.user)

            logger.info(f"Created PMT Config for location {location.name} with cutoff {pmt_cutoff}")
            return {"success": True, "data": config}

        except Exception as e:
            logger.error(f"Error creating PMT Config: {str(e)}", exc_info=True)
            return {"success": False, "error": str(e)}

    def update(self, data):
        """
        Update an existing PMT Configuration.

        Args:
            data: dict with id and fields to update

        Returns:
            dict with success status and error messages
        """
        try:
            config_id = data.get('id')
            if not config_id:
                return {"success": False, "error": "id is required"}

            try:
                config = PmtConfig.objects.get(id=config_id)
            except PmtConfig.DoesNotExist:
                return {"success": False, "error": "PMT Config not found"}

            # Update fields
            if 'pmt_cutoff' in data:
                config.pmt_cutoff = data['pmt_cutoff']
            if 'is_active' in data:
                config.is_active = data['is_active']
            if 'json_ext' in data:
                config.json_ext = data['json_ext']

            config.save(user=self.user)

            logger.info(f"Updated PMT Config {config.id} (location: {config.location.name})")
            return {"success": True, "data": config}

        except Exception as e:
            logger.error(f"Error updating PMT Config: {str(e)}", exc_info=True)
            return {"success": False, "error": str(e)}

    def delete(self, data):
        """
        Delete a PMT Configuration.

        Args:
            data: dict with id

        Returns:
            dict with success status
        """
        try:
            config_id = data.get('id')
            if not config_id:
                return {"success": False, "error": "id is required"}

            try:
                config = PmtConfig.objects.get(id=config_id)
            except PmtConfig.DoesNotExist:
                return {"success": False, "error": "PMT Config not found"}

            config.delete(user=self.user)

            logger.info(f"Deleted PMT Config {config.id}")
            return {"success": True}

        except Exception as e:
            logger.error(f"Error deleting PMT Config: {str(e)}", exc_info=True)
            return {"success": False, "error": str(e)}


class PmtEnrollmentService(BaseService):
    """
    Service for managing PMT Enrollment (CRUD operations).
    """

    def __init__(self, user):
        super().__init__(user)

    def create(self, data):
        """
        Create a new PMT Enrollment record.

        Args:
            data: dict with group_id, pmt_class, pmt_score, status, enrollment_date, beneficiary_id, json_ext

        Returns:
            dict with success status and error messages
        """
        try:
            group_id = data.get('group_id')
            pmt_class = data.get('pmt_class')
            pmt_score = data.get('pmt_score')
            status = data.get('status', PmtEnrollment.Status.PENDING)
            enrollment_date = data.get('enrollment_date', timezone.now())
            beneficiary_id = data.get('beneficiary_id')
            json_ext = data.get('json_ext', {})

            if not group_id:
                return {"success": False, "error": "group_id is required"}
            if not pmt_class:
                return {"success": False, "error": "pmt_class is required"}
            if pmt_score is None:
                return {"success": False, "error": "pmt_score is required"}

            # Verify group exists
            try:
                group = Group.objects.get(id=group_id)
            except Group.DoesNotExist:
                return {"success": False, "error": "Group not found"}

            enrollment = PmtEnrollment(
                group=group,
                pmt_class=pmt_class,
                pmt_score=pmt_score,
                status=status,
                enrollment_date=enrollment_date,
                beneficiary_id=beneficiary_id,
                json_ext=json_ext,
                user_updated=self.user
            )
            enrollment.save(user=self.user)

            logger.info(f"Created PMT Enrollment for group {group.code} (class: {pmt_class})")
            return {"success": True, "data": enrollment}

        except Exception as e:
            logger.error(f"Error creating PMT Enrollment: {str(e)}", exc_info=True)
            return {"success": False, "error": str(e)}

    def update(self, data):
        """
        Update an existing PMT Enrollment record.

        Args:
            data: dict with id and fields to update

        Returns:
            dict with success status
        """
        try:
            enrollment_id = data.get('id')
            if not enrollment_id:
                return {"success": False, "error": "id is required"}

            try:
                enrollment = PmtEnrollment.objects.get(id=enrollment_id)
            except PmtEnrollment.DoesNotExist:
                return {"success": False, "error": "PMT Enrollment not found"}

            # Update fields
            if 'pmt_class' in data:
                enrollment.pmt_class = data['pmt_class']
            if 'pmt_score' in data:
                enrollment.pmt_score = data['pmt_score']
            if 'status' in data:
                enrollment.status = data['status']
            if 'enrollment_date' in data:
                enrollment.enrollment_date = data['enrollment_date']
            if 'beneficiary_id' in data:
                enrollment.beneficiary_id = data['beneficiary_id']
            if 'json_ext' in data:
                enrollment.json_ext = data['json_ext']

            enrollment.save(user=self.user)

            logger.info(f"Updated PMT Enrollment {enrollment.id}")
            return {"success": True, "data": enrollment}

        except Exception as e:
            logger.error(f"Error updating PMT Enrollment: {str(e)}", exc_info=True)
            return {"success": False, "error": str(e)}
