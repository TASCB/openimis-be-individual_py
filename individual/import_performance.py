"""Scoped import optimization; no global flags or changes to approval workflows."""
import csv
import io
import json
import time
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connections

_state = ContextVar('individual_import_performance', default=None)

@contextmanager
def measure_phase(name):
    started = time.monotonic()
    try:
        yield
    finally:
        state = _state.get()
        if state is not None:
            state['timings'][name] = state['timings'].get(name, 0) + time.monotonic() - started

@contextmanager
def optimized_import(service, timings):
    state = {'timings': timings, 'uploads': set()}
    token = _state.set(state)
    try:
        yield
        if state['uploads']:
            with measure_phase('reporting_seconds'):
                try:
                    service.synchronize_data_for_reporting(list(state["uploads"]), _flush=True)
                except Exception:
                    logging.getLogger(__name__).exception("Deferred import reporting failed")
    finally:
        _state.reset(token)

def defer_reporting(upload_id):
    state = _state.get()
    if state is None:
        return False
    state['uploads'].add(upload_id)
    return True

def copy_staging(model, objects):
    """COPY only the existing staging model, retaining model defaults and audit fields.

    Outside an ETL optimization scope or on other databases use the ORM. COPY
    errors propagate to the caller's transaction instead of falling back midway.
    """
    alias = model.objects.db
    connection = connections[alias]
    if _state.get() is None or connection.vendor != 'postgresql':
        model.objects.bulk_create(objects, batch_size=2000)
        return
    fields = list(model._meta.local_concrete_fields)
    stream = io.StringIO()
    writer = csv.writer(stream)
    for obj in objects:
        values = []
        for field in fields:
            value = getattr(obj, field.attname)
            if field.get_internal_type() == 'JSONField' and value is not None:
                value = json.dumps(value, cls=DjangoJSONEncoder, allow_nan=False)
            elif hasattr(value, 'isoformat'):
                value = value.isoformat()
            values.append('\\N' if value is None else str(value))
        writer.writerow(values)
    stream.seek(0)
    quote = connection.ops.quote_name
    sql = 'COPY {} ({}) FROM STDIN WITH (FORMAT CSV, NULL \'\\N\')'.format(
        quote(model._meta.db_table), ', '.join(quote(f.column) for f in fields))
    with connection.cursor() as cursor:
        if hasattr(cursor.cursor, 'copy_expert'):
            cursor.cursor.copy_expert(sql, stream)
        else:
            with cursor.cursor.copy(sql) as copy:
                copy.write(stream.getvalue())

def timed_phase(name):
    from functools import wraps
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with measure_phase(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate
