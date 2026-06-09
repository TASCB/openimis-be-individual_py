import logging

from core.service_signals import ServiceSignalBindType
from core.signals import bind_service_signal
from individual.services import GroupIndividualService, IndividualService, CreateGroupAndMoveIndividualService, \
     GroupService, complete_deduplication_task
from individual.signals.on_validation_import_valid_items import on_task_complete_import_validated, on_task_resolve

from tasks_management.services import on_task_complete_service_handler

logger = logging.getLogger(__name__)


def bind_service_signals():
    def on_task_complete_deduplication(**kwargs):
        from core.models import User
        from individual.apps import IndividualConfig
        from tasks_management.models import Task

        try:
            result = kwargs.get('result')
            if not result or not result.get('success'):
                return

            data = result.get('data') or {}
            task_payload = data.get('task') or {}
            if task_payload.get('business_event') != IndividualConfig.deduplication_review_event:
                return
            if task_payload.get('status') != Task.Status.COMPLETED:
                return

            task = Task.objects.get(id=task_payload.get('id'))
            user = User.objects.get(id=data.get('user', {}).get('id'))
            complete_deduplication_task(task, user, task_payload)
        except Exception as exc:
            logger.error("Error while completing individual deduplication task", exc_info=exc)

    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_service_handler(GroupIndividualService),
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_service_handler(IndividualService),
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_service_handler(GroupService),
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_service_handler(CreateGroupAndMoveIndividualService),
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_import_validated,
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_deduplication,
        bind_type=ServiceSignalBindType.AFTER
    )
    from individual.pmt_service import PmtGlobalFormulaService
    bind_service_signal(
        'task_service.complete_task',
        on_task_complete_service_handler(PmtGlobalFormulaService),
        bind_type=ServiceSignalBindType.AFTER
    )
    bind_service_signal(
        'task_service.resolve_task',
        on_task_resolve,
        bind_type=ServiceSignalBindType.AFTER
    )
