import logging
import uuid

from django.db import connection, transaction
from django.utils import timezone

from individual.apps import IndividualConfig
from individual.gql_queries import filter_by_pmt_class, non_consented_household_q, pct_enrolled_q
from individual.models import Group, GroupIndividual, IndividualDataSource, IndividualDataSourceUpload, PmtEnrollment

logger = logging.getLogger(__name__)

TASK_SOURCE = "pct_enrolment"
DISTRICT_SOURCE_TYPE = "pct enrolment district"


def upload_enrolment_candidates(upload_id):
    individuals = IndividualDataSource.objects.filter(
        upload_id=upload_id, individual__isnull=False,
    ).values("individual_id")
    groups = Group.objects.filter(
        is_deleted=False,
        id__in=GroupIndividual.objects.filter(
            is_deleted=False, individual_id__in=individuals,
        ).values("group_id"),
    )
    return (
        filter_by_pmt_class(groups, "POOR")
        .exclude(non_consented_household_q())
        .exclude(pct_enrolled_q())
    )


def district_enrolment_candidates(district_code):
    from individual.pmt_service import PmtService

    groups = Group.objects.filter(
        is_deleted=False, location_id__in=PmtService._location_and_descendant_ids(district_code),
    )
    return (
        filter_by_pmt_class(groups, "POOR")
        .exclude(non_consented_household_q())
        .exclude(pct_enrolled_q())
    )


def district_codes(groups):
    from location.models import Location

    location_ids = set(groups.values_list("location_id", flat=True)) - {None}
    codes = set()
    while location_ids:
        rows = Location.objects.filter(id__in=location_ids).values_list("type", "code", "parent_id")
        codes |= {code for type_, code, _ in rows if type_ == "D"}
        location_ids = {parent for type_, _, parent in rows if type_ != "D" and parent}
    return sorted(codes)


def pct_programme_exists():
    from social_protection.models import BenefitPlan

    return BenefitPlan.objects.filter(
        code=(IndividualConfig.pct_benefit_plan_code or "").strip(),
        type=BenefitPlan.BenefitPlanType.GROUP_TYPE,
        is_deleted=False,
    ).exists()


class PctEnrolmentService:

    def __init__(self, user):
        self.user = user

    def raise_for_upload(self, upload_id):
        if not (IndividualConfig.pct_auto_enroll_enabled and IndividualConfig.pct_enroll_on_import):
            return None
        if not pct_programme_exists():
            logger.error(
                "No PCT enrolment task for upload %s: programme %s is missing. Run manage.py create_pct_programme.",
                upload_id, IndividualConfig.pct_benefit_plan_code,
            )
            return {"success": False, "detail": "pct_benefit_plan_not_found"}
        candidates = upload_enrolment_candidates(upload_id)
        if not candidates.exists():
            return None
        if not IndividualConfig.enable_maker_checker_for_pct_enrolment:
            return self.enrol_upload(upload_id)
        return {code: self.raise_for_district(code) for code in district_codes(candidates)}

    def raise_for_district(self, district_code, dry_run=False):
        from tasks_management.models import Task

        if not IndividualConfig.pct_auto_enroll_enabled:
            return {"success": False, "detail": "pct_auto_enroll_disabled"}
        if not pct_programme_exists():
            return {"success": False, "detail": "pct_benefit_plan_not_found"}
        code = str(district_code)

        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [f"pct_enrolment:{code}"])
            all_open = list(Task.objects.filter(
                is_deleted=False,
                business_event=IndividualConfig.pct_enrolment_task_event,
                status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
            ))
            open_tasks = [t for t in all_open if (t.json_ext or {}).get("district_code") == code]
            staged = IndividualDataSource.objects.filter(
                upload_id__in=[(t.json_ext or {}).get("upload_id") for t in all_open],
            ).values("individual_id")
            heads = list(GroupIndividual.objects.filter(
                is_deleted=False, role=GroupIndividual.Role.HEAD,
                group_id__in=district_enrolment_candidates(code).values("id"),
            ).exclude(individual_id__in=staged).values_list("individual_id", "group_id"))
            receiving = next((t for t in open_tasks if t.status == Task.Status.RECEIVED), None)

            if not heads or dry_run:
                result = {"success": True, "households": len(heads), "dry_run": dry_run}
                if open_tasks and not heads:
                    result.update(existing=True, task_id=str(open_tasks[0].id))
                return result
            if receiving:
                self._stage(receiving.json_ext["upload_id"], heads, code)
                return self._refresh_task(receiving, added=len(heads))

            upload = IndividualDataSourceUpload(
                source_name=f"PCT enrolment, district {code}, {timezone.now():%Y-%m-%d}",
                source_type=DISTRICT_SOURCE_TYPE,
                status=IndividualDataSourceUpload.Status.SUCCESS,
            )
            upload.save(user=self.user)
            self._stage(upload.id, heads, code)
            if not IndividualConfig.enable_maker_checker_for_pct_enrolment:
                return self.enrol_upload(upload.id)
            return self._create_task(upload.id, len(heads), district_code=code)

    def _stage(self, upload_id, heads, district_code):
        IndividualDataSource.objects.bulk_create([
            IndividualDataSource(
                id=uuid.uuid4(), upload_id=upload_id, individual_id=individual_id,
                json_ext={"group_id": str(group_id), "district_code": district_code},
                validations={}, user_created=self.user, user_updated=self.user,
            )
            for individual_id, group_id in heads
        ])

    @staticmethod
    def _staged_counts(upload_id):
        group_ids = GroupIndividual.objects.filter(
            is_deleted=False, role=GroupIndividual.Role.HEAD,
            individual_id__in=IndividualDataSource.objects.filter(upload_id=upload_id).values("individual_id"),
        ).values("group_id")
        households = IndividualDataSource.objects.filter(upload_id=upload_id).count()
        members = GroupIndividual.objects.filter(is_deleted=False, group_id__in=group_ids).count()
        return households, members

    def _refresh_task(self, task, added):
        households, members = self._staged_counts(task.json_ext["upload_id"])
        task.json_ext = {**task.json_ext, "households": households, "members": members}
        task.save(user=self.user)
        return {"success": True, "task_id": str(task.id), "added": added, "households": households}

    def _create_task(self, upload_id, households, district_code):
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.models import Task
        from tasks_management.services import TaskService

        upload = IndividualDataSourceUpload.objects.get(id=upload_id)
        _, members = self._staged_counts(upload_id)
        return TaskService(self.user).create({
            "source": TASK_SOURCE,
            "entity": upload,
            "status": Task.Status.RECEIVED,
            "executor_action_event": TasksManagementConfig.default_executor_event,
            "business_event": IndividualConfig.pct_enrolment_task_event,
            "json_ext": {
                "upload_id": str(upload_id),
                "source_name": upload.source_name,
                "households": households,
                "members": members,
                "benefit_plan_code": IndividualConfig.pct_benefit_plan_code,
                "district_code": district_code,
            },
        })

    def enrol_upload(self, upload_id):
        from individual.pmt_service import PctAutoEnrollmentService

        groups = list(upload_enrolment_candidates(upload_id))
        group_ids = [g.id for g in groups]
        with_enrolment = set(PmtEnrollment.objects.filter(
            group_id__in=group_ids,
            is_deleted=False,
            status__in=[PmtEnrollment.Status.PENDING, PmtEnrollment.Status.ENROLLED],
        ).values_list("group_id", flat=True))

        now = timezone.now()
        created = 0
        with transaction.atomic():
            for group in groups:
                score = (group.json_ext or {}).get("pmt_score_household")
                if group.id in with_enrolment or score is None:
                    continue
                PmtEnrollment(
                    group=group,
                    pmt_class=PmtEnrollment.PmtClass.POOR,
                    pmt_score=score,
                    status=PmtEnrollment.Status.PENDING,
                    enrollment_date=now,
                    json_ext={"trigger": "import", "upload_id": str(upload_id), "created_by": str(self.user.id)},
                ).save(user=self.user)
                created += 1
            result = PctAutoEnrollmentService(self.user).sync_pending_poor_households(group_ids=group_ids)
        logger.info("PCT enrolment for upload %s: %s new enrolment(s), sync %s", upload_id, created, result)
        return {**result, "enrolments_created": created}


def on_task_complete_pct_enrolment(**kwargs):
    from core.models import User
    from tasks_management.models import Task

    result = kwargs.get("result") or {}
    if not result.get("success"):
        return
    data = result.get("data") or {}
    task = data.get("task") or {}
    if task.get("business_event") != IndividualConfig.pct_enrolment_task_event:
        return
    if task.get("status") != Task.Status.COMPLETED:
        return
    try:
        upload_id = Task.objects.get(id=task["id"]).json_ext["upload_id"]
        user = User.objects.get(id=data["user"]["id"])
        PctEnrolmentService(user).enrol_upload(upload_id)
    except Exception as exc:
        logger.error("PCT enrolment after task approval failed", exc_info=exc)
