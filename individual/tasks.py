from __future__ import absolute_import, unicode_literals
import logging
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=0, name="individual.rerun_pmt")
def rerun_pmt_task(self, district_code, region_code, pmt_cutoff, user_id, mutation_id):
    """
    Async Celery task for PMT rerun.
    Dispatched by RerunPmtMutation instead of running synchronously.
    Progress is tracked via PmtRunProgress model (polled by frontend).
    """
    from core.models import User
    from individual.pmt_service import PmtService
    from individual.models import PmtRunProgress
    from django.utils import timezone

    logger.info(
        f"[PMT Task] Starting: district={district_code}, "
        f"cutoff={pmt_cutoff}, mutation_id={mutation_id}"
    )

    # Mark as started
    try:
        PmtRunProgress.objects.update_or_create(
            mutation_id=mutation_id,
            defaults={
                "status": PmtRunProgress.Status.STARTED,
                "district_code": district_code,
            }
        )
    except Exception as e:
        logger.warning(f"[PMT Task] Could not init progress: {e}")

    try:
        user = User.objects.get(id=user_id)
        service = PmtService(user)

        result = service.rerun_pmt(
            district_code=district_code,
            region_code=region_code,
            pmt_cutoff=pmt_cutoff,
            mutation_id=mutation_id,
        )

        logger.info(
            f"[PMT Task] Completed: "
            f"{result.get('updated_groups', 0)} groups, "
            f"{result.get('updated_individuals', 0)} individuals"
        )

        # Mark as completed if service didn't already do it
        try:
            progress = PmtRunProgress.objects.get(mutation_id=mutation_id)
            if progress.status != PmtRunProgress.Status.COMPLETED:
                progress.status = PmtRunProgress.Status.COMPLETED
                progress.completed_at = timezone.now()
                progress.save()
        except PmtRunProgress.DoesNotExist:
            pass

        return result

    except Exception as e:
        logger.error(f"[PMT Task] Failed: {str(e)}", exc_info=True)

        # Mark as failed
        try:
            PmtRunProgress.objects.filter(
                mutation_id=mutation_id
            ).update(
                status=PmtRunProgress.Status.FAILED,
                completed_at=timezone.now()
            )
        except Exception:
            pass

        raise


@shared_task(bind=True, max_retries=0, name="individual.adjust_pmt_cutoff")
def adjust_pmt_cutoff_task(self, district_code, region_code, pmt_cutoff, user_id, mutation_id):
    """
    Async Celery task for cutoff-only PMT adjustment.
    Reuses stored PMT scores and only reclassifies households / members.
    """
    from core.models import User
    from individual.pmt_service import PmtService
    from individual.models import PmtRunProgress
    from django.utils import timezone

    logger.info(
        f"[PMT Cutoff Adjustment Task] Starting: district={district_code}, "
        f"cutoff={pmt_cutoff}, mutation_id={mutation_id}"
    )

    try:
        PmtRunProgress.objects.update_or_create(
            mutation_id=mutation_id,
            defaults={
                "status": PmtRunProgress.Status.STARTED,
                "district_code": district_code,
            }
        )
    except Exception as e:
        logger.warning(f"[PMT Cutoff Adjustment Task] Could not init progress: {e}")

    try:
        user = User.objects.get(id=user_id)
        service = PmtService(user)

        result = service.adjust_pmt_cutoff(
            district_code=district_code,
            region_code=region_code,
            pmt_cutoff=pmt_cutoff,
            mutation_id=mutation_id,
        )

        logger.info(
            f"[PMT Cutoff Adjustment Task] Completed: "
            f"{result.get('updated_groups', 0)} groups, "
            f"{result.get('updated_individuals', 0)} individuals"
        )

        try:
            progress = PmtRunProgress.objects.get(mutation_id=mutation_id)
            if progress.status != PmtRunProgress.Status.COMPLETED:
                progress.status = PmtRunProgress.Status.COMPLETED
                progress.completed_at = timezone.now()
                progress.save()
        except PmtRunProgress.DoesNotExist:
            pass

        return result

    except Exception as e:
        logger.error(f"[PMT Cutoff Adjustment Task] Failed: {str(e)}", exc_info=True)

        try:
            PmtRunProgress.objects.filter(
                mutation_id=mutation_id
            ).update(
                status=PmtRunProgress.Status.FAILED,
                completed_at=timezone.now()
            )
        except Exception:
            pass

        raise
