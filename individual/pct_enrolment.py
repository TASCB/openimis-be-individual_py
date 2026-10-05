import logging

from django.db import transaction
from django.utils import timezone

from individual.apps import IndividualConfig
from individual.gql_queries import filter_by_pmt_class, non_consented_household_q, pct_enrolled_q
from individual.models import Group, GroupIndividual, IndividualDataSource, IndividualDataSourceUpload, PmtEnrollment

logger = logging.getLogger(__name__)

TASK_SOURCE = "pct_enrolment"


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
        households = candidates.count()
        if not households:
            return None
        if not IndividualConfig.enable_maker_checker_for_pct_enrolment:
            return self.enrol_upload(upload_id)
        return self._create_task(upload_id, candidates, households)

    def _create_task(self, upload_id, candidates, households):
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.models import Task
        from tasks_management.services import TaskService

        open_task = Task.objects.filter(
            is_deleted=False,
            business_event=IndividualConfig.pct_enrolment_task_event,
            json_ext__upload_id=str(upload_id),
            status__in=[Task.Status.RECEIVED, Task.Status.ACCEPTED],
        ).first()
        if open_task:
            return {"success": True, "task_id": str(open_task.id), "existing": True}

        upload = IndividualDataSourceUpload.objects.get(id=upload_id)
        members = GroupIndividual.objects.filter(
            is_deleted=False, group_id__in=candidates.values("id"),
        ).count()
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
