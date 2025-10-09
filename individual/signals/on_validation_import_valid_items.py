import logging
import random
import string
from typing import List
from django.contrib.postgres.aggregates import ArrayAgg
from django.db.models import F, Q
from core.models import User
from individual.apps import IndividualConfig
from individual.models import (
    IndividualDataSourceUpload,
    IndividualDataSource,
    IndividualDataUploadRecords, Group, GroupIndividual, Individual, GroupDataSource
)
from individual.services import GroupIndividualService, GroupService
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task
from tasks_management.services import TaskService
from workflow.services import WorkflowService

logger = logging.getLogger(__name__)


class ItemsUploadTaskCompletionEvent:
    def run_workflow(self):
        group, name = self.workflow_name.split('.')
        workflow = self._get_workflow(group, name)
        result = workflow.run({
            'user_uuid': str(self.user.id),
            'upload_uuid': str(self.upload_id),
            'accepted': self.accepted
        })
        if not result.get('success'):
            if self.upload_record:
                data_upload = self.upload_record.data_upload
                data_upload.status = IndividualDataSourceUpload.Status.FAIL
                data_upload.error = {"Task Resolve": str(result.get('message'))}
                # Todo: this should be changed to system user
                data_upload.save(username=data_upload.user_updated.username)

    def _get_workflow(self, group, name):
        result_workflow = WorkflowService.get_workflows(name, group)
        if not result_workflow.get('success'):
            raise ValueError('{}: {}'.format(result_workflow.get("message"), result_workflow.get("details")))
        workflows = result_workflow.get('data', {}).get('workflows')
        if not workflows:
            raise ValueError('Workflow not found: group={} name={}'.format(group, name))
        if len(workflows) > 1:
            raise ValueError('Multiple workflows found: group={} name={}'.format(group, name))
        workflow = workflows[0]
        return workflow

    def __init__(self, workflow: str, upload_record, upload_id: str, user: User, accepted: List[str] = None):
        """
        Workflow name should be in workflow_group.workflow_name notation.
        Upload ID is IndividualDataSource upload id.
        User is actor performing action.
        """
        self.workflow_name = workflow
        self.upload_record = upload_record
        self.upload_id = upload_id
        self.user = user
        self.accepted = accepted


class BaseGroupColumnAggregationClass(ItemsUploadTaskCompletionEvent):
    group_code_str = 'group_code'
    recipient_info_str = 'recipient_info'
    individual_role_str = 'individual_role'
    individuals = None
    group_aggregation_column = None
    grouped_individuals = None

    def run_workflow(self):
        super().run_workflow()

        if not self.upload_record:
            return

        upload_record_json_ext = self._get_json_ext(self.upload_record)
        group_aggregation_column = upload_record_json_ext.get('group_aggregation_column', self.group_code_str)
        self.set_group_aggregation_column(group_aggregation_column)
        self.individuals = self._query_individuals()
        self.grouped_individuals = self._get_grouped_individuals()

    def _create_task(self):
        json_ext = {
            'source_name': self.upload_record.data_upload.source_name,
            'workflow': self.upload_record.workflow,
            'data_upload_id': str(self.upload_record.data_upload.id),
            'group_aggregation_column':
                self.upload_record.json_ext.get('group_aggregation_column')
                if isinstance(self.upload_record.json_ext, dict)
                else None,
        }
        TaskService(self.user).create({
            'source': 'import_group_valid_items',
            'entity': self.upload_record,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': IndividualConfig.validation_import_group_valid_items,
            'json_ext': json_ext
        })

        data_upload = self.upload_record.data_upload
        data_upload.status = IndividualDataSourceUpload.Status.WAITING_FOR_VERIFICATION
        data_upload.save(user=self.user)

    @staticmethod
    def group_data_sources_into_entities(upload_id, user, accepted: List[str] = None):
        data_sources = GroupDataSource.objects.filter(upload_id=upload_id, group=None)

        if accepted:
            data_sources = data_sources.filter(id__in=accepted)

        service = GroupService(user)
        for source in data_sources:
            obj_data = source.json_ext
            if obj_data.get('id'):
                result = service.update(obj_data)
            else:
                result = service.create(obj_data)

            group_id = result["data"].get('id')
            if group_id:
                source.group_id = group_id
                source.save(username=user.username)

    def set_group_aggregation_column(self, group_aggregation_column):
        if group_aggregation_column == 'null' or not group_aggregation_column:
            self.group_aggregation_column = self.group_code_str
        else:
            self.group_aggregation_column = group_aggregation_column

    def _get_grouped_individuals(self):
        return (
            self.individuals
            .exclude(**{f'json_ext__{self.group_aggregation_column}__isnull': True})
            .exclude(**{f'json_ext__{self.group_aggregation_column}': ''})
            .exclude(**{f'json_ext__{self.group_aggregation_column}': None})
            .values(f'json_ext__{self.group_aggregation_column}')
            .annotate(
                record_ids=ArrayAgg('id'),
                value=F(f'json_ext__{self.group_aggregation_column}')
            )
        )

    def _clean_json_ext(self):
        def clean_json_ext(json_ext):
            if json_ext is None:
                return None
            json_ext.pop(self.group_code_str, None)
            json_ext.pop(self.recipient_info_str, None)
            json_ext.pop(self.individual_role_str, None)
            return json_ext

        for individual in self.individuals:
            original_json_ext = self._get_json_ext(individual)
            cleaned_json_ext = clean_json_ext(original_json_ext.copy() if original_json_ext else None)
            if cleaned_json_ext != original_json_ext:
                individual.json_ext = cleaned_json_ext
                individual.save(user=self.user)

    def _query_individuals(self):
        return Individual.objects.filter(
            individualdatasource__upload__id=self.upload_id, is_deleted=False, individualdatasource__is_deleted=False
        )

    @staticmethod
    def _get_json_ext(instance):
        if not hasattr(instance, 'json_ext'):
            return {}
        return instance.json_ext or {}

    def _create_or_update_groups_using_group_code(self):
        for individual_group in self.grouped_individuals:
            ids = individual_group['record_ids']
            ids_str = [str(uuid) for uuid in ids]
            group_code = individual_group['value']
            group = Group.objects.filter(code=group_code).first()

            if group:
                assigned_individual_ids = list(Individual.objects.filter(
                    groupindividuals__group=group, is_deleted=False
                ).values_list('id', flat=True))
                assigned_individual_ids_str = [str(uuid) for uuid in assigned_individual_ids]
                updated_ids = list(set(ids_str + assigned_individual_ids_str))
                update_individuals_data = self._build_individual_data(updated_ids)
                obj_data = {"id": str(group.id), "individuals_data": update_individuals_data, "code": group_code}
            else:
                individuals_data = self._build_individual_data(ids_str)
                obj_data = {"individuals_data": individuals_data, "code": group_code}

            self._create_group_data_source(obj_data)

    def _build_individual_data(self, ids):
        def build_single_individual_data(individual_id):
            individual = Individual.objects.get(id=individual_id)
            individual_json_ext = self._get_json_ext(individual)
            recipient_info = individual_json_ext.get('recipient_info')
            individual_role = individual_json_ext.get(self.individual_role_str)
            individual_role = self._individual_role_parser(individual_role)
            recipient_type = self._recipient_type_parser(recipient_info)
            return {'individual_id': individual_id, 'recipient_type': recipient_type, 'role': individual_role}

        return [build_single_individual_data(individual_id) for individual_id in ids]

    @staticmethod
    def _recipient_type_parser(recipient_type):
        if recipient_type in [1, '1', 1.0]:
            return GroupIndividual.RecipientType.PRIMARY
        if recipient_type in [2, '2', 2.0]:
            return GroupIndividual.RecipientType.SECONDARY
        return None
    
    @staticmethod
    def _individual_role_parser(individual_role):
        return getattr(GroupIndividual.Role, individual_role.upper(), None)

    def _create_group_data_source(self, json_ext_data):
        data_source = GroupDataSource(upload=self.upload_record.data_upload, json_ext=json_ext_data)
        data_source.save(user=self.user)

    def _create_groups(self):
        for individual_group in self.grouped_individuals:
            ids = individual_group['record_ids']
            ids_str = [str(uuid) for uuid in ids]
            code = self.generate_unique_code()
            individuals_data = self._build_individual_data(ids_str)
            obj_data = {"individuals_data": individuals_data, "code": code}
            self._create_group_data_source(obj_data)

    @staticmethod
    def generate_unique_code():
        """Generate a unique 8-digit code."""
        while True:
            code = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
            if not Group.objects.filter(code=code).exists():
                return code

    def _create_task_or_data_source_into_entity(self):
        if IndividualConfig.enable_maker_checker_for_group_upload:
            self._create_task()
        else:
            self.group_data_sources_into_entities(self.upload_record.data_upload.id, self.user)


class IndividualItemsImportTaskCompletionEvent(BaseGroupColumnAggregationClass):

    def run_workflow(self):
        super().run_workflow()

        if self.grouped_individuals is None or not self.grouped_individuals.exists():
            return

        if self.group_aggregation_column == self.group_code_str:
            self._create_or_update_groups_using_group_code()
        else:
            self._create_groups()

        self._create_task_or_data_source_into_entity()
        self._clean_json_ext()


class IndividualItemsUploadTaskCompletionEvent(BaseGroupColumnAggregationClass):
    def run_workflow(self):
        super().run_workflow()

        if self.grouped_individuals is None or not self.grouped_individuals.exists():
            return

        self._create_or_update_groups_using_group_code()
        self._create_task_or_data_source_into_entity()
        self._clean_json_ext()


def on_task_complete_action(business_event, **kwargs):
    from individual.apps import IndividualConfig

    result = kwargs.get('result')
    if not result or not result.get('success'):
        return

    data = result.get('data')
    task = data.get('task') if data else None
    # Further conditions for early return
    if not task or task.get('business_event') != business_event:
        return

    task_status = task.get('status')
    if task_status != Task.Status.COMPLETED:
        return

    # Main logic remains unchanged, assuming necessary variables are correctly set
    upload_record = None
    try:
        upload_record = IndividualDataUploadRecords.objects.get(id=task['entity_id'])
        if business_event == IndividualConfig.validation_import_valid_items:
            workflow = IndividualConfig.validation_import_valid_items_workflow
            IndividualItemsImportTaskCompletionEvent(
                workflow,
                upload_record,
                upload_record.data_upload.id,
                User.objects.get(id=data['user']['id'])
            ).run_workflow()
        elif business_event == IndividualConfig.validation_upload_valid_items:
            workflow = IndividualConfig.validation_upload_valid_items_workflow
            IndividualItemsUploadTaskCompletionEvent(
                workflow,
                upload_record,
                upload_record.data_upload.id,
                User.objects.get(id=data['user']['id'])
            ).run_workflow()
        elif business_event == IndividualConfig.validation_import_group_valid_items:
            BaseGroupColumnAggregationClass.group_data_sources_into_entities(
                upload_record.data_upload.id, User.objects.get(id=data['user']['id'])
            )
        else:
            raise ValueError(f"Business event {business_event} doesn't have assigned workflow.")
    except Exception as exc:
        if upload_record:
            data_upload = upload_record.data_upload
            data_upload.status = IndividualDataSourceUpload.Status.FAIL
            data_upload.error = {"Task Resolve": str(exc)}
            # Todo: this should be changed to system user
            data_upload.save(username=data_upload.user_updated.username)
        logger.error(f"Error while executing on_task_complete_action for {business_event}", exc_info=exc)


def on_task_complete_import_validated(**kwargs):
    from individual.apps import IndividualConfig
    on_task_complete_action(IndividualConfig.validation_import_valid_items, **kwargs)
    on_task_complete_action(IndividualConfig.validation_upload_valid_items, **kwargs)
    on_task_complete_action(IndividualConfig.validation_import_group_valid_items, **kwargs)


def _delete_rejected(uuids_list, task_source):
    # Use soft delete to remove atomic tasks, it's not possible to mark them on level of Individual.
    model = IndividualDataSource
    if task_source == 'import_group_valid_items':
        model = GroupDataSource

    sources_to_update = model.objects.filter(id__in=uuids_list)

    # Set is_deleted to True for each instance
    for source in sources_to_update:
        source.is_deleted = True

    # Perform the bulk update
    model.objects.bulk_update(sources_to_update, ['is_deleted'])


def _complete_task_for_accepted(_task, accept, user):
    from individual.apps import IndividualConfig
    upload_record = IndividualDataUploadRecords.objects.get(id=_task.entity_id)

    if not upload_record:
        return

    if _task.business_event == IndividualConfig.validation_import_valid_items:
        IndividualItemsImportTaskCompletionEvent(
            IndividualConfig.validation_import_valid_items_workflow,
            upload_record,
            upload_record.data_upload.id,
            user,
            accept
        ).run_workflow()

    if _task.business_event == IndividualConfig.validation_upload_valid_items:
        IndividualItemsUploadTaskCompletionEvent(
            IndividualConfig.validation_upload_valid_items_workflow,
            upload_record,
            upload_record.data_upload.id,
            user,
            accept
        ).run_workflow()

    if _task.business_event == IndividualConfig.validation_import_group_valid_items:
        BaseGroupColumnAggregationClass.group_data_sources_into_entities(
            upload_record.data_upload.id, user, accept
        )


def _resolve_task_any(_task: Task, _user):
    # Atomic resolution of individuals
    user_id_str = str(_user.id)
    if isinstance(_task.business_status.get(user_id_str), dict):
        last = _task.history.first().prev_record
        if last and isinstance(last.business_status.get(user_id_str), dict):
            # Only new approvals/rejections, the format is {user_id: {[ACCEPT|REJECT]: [uuid1_, ... uuid_n]}
            accept = list(set(_task.business_status[user_id_str].get('ACCEPT', []))
                          - set(last.business_status[user_id_str].get('ACCEPT', [])))
            reject = list(set(_task.business_status[user_id_str].get('REJECT', []))
                          - set(last.business_status[user_id_str].get('REJECT', [])))
        else:
            accept = _task.business_status[user_id_str].get('ACCEPT', [])
            reject = _task.business_status[user_id_str].get('REJECT', [])

        _delete_rejected(reject, _task.source)
        _complete_task_for_accepted(_task, accept, _user)


def _resolve_task_all(_task, _user):
    # TODO for now hardcoded to any, to be updated
    _resolve_task_any(_task, _user)


def _resolve_task_n(_task, _user):
    # TODO for now hardcoded to any, to be updated
    _resolve_task_any(_task, _user)


def on_task_resolve(**kwargs):
    from tasks_management.apps import TasksManagementConfig
    from individual.apps import IndividualConfig
    """
    Partial approval requires custom resolve policy that doesn't rely on default APPROVE value in businessStatus.
    """
    try:
        result = kwargs.get('result', None)
        task_data = result['data']['task']
        if result and result['success'] \
                and task_data['status'] == Task.Status.ACCEPTED \
                and task_data['executor_action_event'] == TasksManagementConfig.default_executor_event \
                and task_data['business_event'] in [
            IndividualConfig.validation_import_valid_items,
            IndividualConfig.validation_upload_valid_items,
            IndividualConfig.validation_import_group_valid_items
        ]:
            data = kwargs.get("result").get("data")
            task = Task.objects.select_related('task_group').prefetch_related('task_group__taskexecutor_set').get(
                id=data["task"]["id"])
            user = User.objects.get(id=data["user"]["id"])

            # Task only relevant for this specific source
            if task.source != 'import_valid_items' and task.source != 'import_group_valid_items':
                return

            if not task.task_group:
                logger.error("Resolving task not assigned to TaskGroup: %s", data['task']['id'])
                return ['Task not assigned to TaskGroup']

            resolvers = {
                'ALL': _resolve_task_all,
                'ANY': _resolve_task_any,
                'N': _resolve_task_n,
            }

            if task.task_group.completion_policy not in resolvers:
                logger.error("Resolving task with unknown completion_policy: %s", task.task_group.completion_policy)
                return ['Unknown completion_policy: %s' % task.task_group.completion_policy]

            resolvers[task.task_group.completion_policy](task, user)
    except Exception as e:
        logger.error("Error while executing on_task_resolve", exc_info=e)
        return [str(e)]
