# Generated migration to flatten nested json_ext structure
# This migration converts from nested structure:
#   {json_ext: {consent_res: "2", ...}}
# To flat structure:
#   {consent_res: "2", ...}

import logging
from django.db import migrations

logger = logging.getLogger(__name__)


def flatten_json_ext(apps, schema_editor):
    """
    Flatten nested json_ext structure to match flat structure from ETL.

    Converts from:
        {
            "json_ext": {
                "consent_res": "2",
                "external_id": "...",
                "raw": {...}
            },
            "pmt_score": 45.2
        }

    To:
        {
            "consent_res": "2",
            "external_id": "...",
            "pmt_score": 45.2,
            "raw": {...}
        }
    """
    Individual = apps.get_model('individual', 'Individual')
    Group = apps.get_model('individual', 'Group')

    # Track statistics
    stats = {
        'individual_updated': 0,
        'individual_error': 0,
        'group_updated': 0,
        'group_error': 0,
        'skipped_no_nested': 0,
        'skipped_no_json_ext': 0,
    }

    # ==================== Individual Records ====================
    for individual in Individual.objects.all():
        try:
            json_ext = individual.json_ext

            # Skip if no json_ext
            if not json_ext:
                stats['skipped_no_json_ext'] += 1
                continue

            # Check if already flat (no nested json_ext layer)
            if 'json_ext' not in json_ext:
                stats['skipped_no_nested'] += 1
                continue

            # Extract nested layer
            nested = json_ext.pop('json_ext')

            if not isinstance(nested, dict):
                logger.warning(
                    f"Individual {individual.id}: json_ext.json_ext is not a dict, skipping"
                )
                stats['individual_error'] += 1
                continue

            # Promote all nested fields to top level (except duplicates)
            for key, value in nested.items():
                if key not in json_ext:  # Don't overwrite existing top-level values
                    json_ext[key] = value

            # Save updated record
            individual.json_ext = json_ext
            individual.save(update_fields=['json_ext'])
            stats['individual_updated'] += 1

        except Exception as e:
            logger.error(
                f"Error processing Individual {individual.id}: {str(e)}",
                exc_info=True
            )
            stats['individual_error'] += 1

    # ==================== Group Records ====================
    for group in Group.objects.all():
        try:
            json_ext = group.json_ext

            # Skip if no json_ext
            if not json_ext:
                stats['skipped_no_json_ext'] += 1
                continue

            # Check if already flat
            if 'json_ext' not in json_ext:
                stats['skipped_no_nested'] += 1
                continue

            # Extract nested layer
            nested = json_ext.pop('json_ext')

            if not isinstance(nested, dict):
                logger.warning(
                    f"Group {group.id}: json_ext.json_ext is not a dict, skipping"
                )
                stats['group_error'] += 1
                continue

            # Promote all nested fields to top level (except duplicates)
            for key, value in nested.items():
                if key not in json_ext:
                    json_ext[key] = value

            # Save updated record
            group.json_ext = json_ext
            group.save(update_fields=['json_ext'])
            stats['group_updated'] += 1

        except Exception as e:
            logger.error(
                f"Error processing Group {group.id}: {str(e)}",
                exc_info=True
            )
            stats['group_error'] += 1

    # Log final statistics
    logger.info("=" * 70)
    logger.info("JSON_EXT FLATTEN MIGRATION COMPLETE")
    logger.info("=" * 70)
    logger.info(f"Individual records updated: {stats['individual_updated']}")
    logger.info(f"Individual records with errors: {stats['individual_error']}")
    logger.info(f"Group records updated: {stats['group_updated']}")
    logger.info(f"Group records with errors: {stats['group_error']}")
    logger.info(f"Records skipped (no nested layer): {stats['skipped_no_nested']}")
    logger.info(f"Records skipped (no json_ext): {stats['skipped_no_json_ext']}")
    logger.info("=" * 70)

    # Print to stdout as well
    print("\n" + "=" * 70)
    print("JSON_EXT FLATTEN MIGRATION COMPLETE")
    print("=" * 70)
    print(f"Individual records updated: {stats['individual_updated']}")
    print(f"Individual records with errors: {stats['individual_error']}")
    print(f"Group records updated: {stats['group_updated']}")
    print(f"Group records with errors: {stats['group_error']}")
    print(f"Records skipped (no nested layer): {stats['skipped_no_nested']}")
    print(f"Records skipped (no json_ext): {stats['skipped_no_json_ext']}")
    print("=" * 70 + "\n")


def reverse_flatten_json_ext(apps, schema_editor):
    """
    Reverse operation: Re-nest json_ext structure.

    WARNING: This is a best-effort reversal. If targeting keys were mixed
    with raw data, the reversal may not be perfect.
    """
    Individual = apps.get_model('individual', 'Individual')
    Group = apps.get_model('individual', 'Group')

    # Fields that should stay at top level (not re-nest)
    TOP_LEVEL_FIELDS = {
        'pmt_score', 'pmt_class', 'pmt_score_json', 'pmt_class_json',
        'ss_batch', 'dob_missing'
    }

    stats = {
        'individual_reversed': 0,
        'group_reversed': 0,
    }

    # Re-nest Individual records
    for individual in Individual.objects.all():
        try:
            json_ext = individual.json_ext or {}

            # Skip if already nested
            if 'json_ext' in json_ext and isinstance(json_ext.get('json_ext'), dict):
                continue

            # Extract fields that should be nested
            nested = {}
            for key in list(json_ext.keys()):
                if key not in TOP_LEVEL_FIELDS and key != 'raw':
                    nested[key] = json_ext.pop(key)

            # Add 'raw' if it exists
            if 'raw' in json_ext:
                nested['raw'] = json_ext.pop('raw')

            # Put nested back
            if nested:
                json_ext['json_ext'] = nested

            individual.json_ext = json_ext
            individual.save(update_fields=['json_ext'])
            stats['individual_reversed'] += 1
        except Exception as e:
            logger.error(f"Error reversing Individual {individual.id}: {str(e)}", exc_info=True)

    # Re-nest Group records
    for group in Group.objects.all():
        try:
            json_ext = group.json_ext or {}

            if 'json_ext' in json_ext and isinstance(json_ext.get('json_ext'), dict):
                continue

            nested = {}
            for key in list(json_ext.keys()):
                if key not in TOP_LEVEL_FIELDS and key != 'raw':
                    nested[key] = json_ext.pop(key)

            if 'raw' in json_ext:
                nested['raw'] = json_ext.pop('raw')

            if nested:
                json_ext['json_ext'] = nested

            group.json_ext = json_ext
            group.save(update_fields=['json_ext'])
            stats['group_reversed'] += 1
        except Exception as e:
            logger.error(f"Error reversing Group {group.id}: {str(e)}", exc_info=True)

    logger.info(f"Reversed Individual records: {stats['individual_reversed']}")
    logger.info(f"Reversed Group records: {stats['group_reversed']}")


class Migration(migrations.Migration):

    dependencies = [
        ('individual', '0018_alter_groupindividual_role_and_more'),
    ]

    operations = [
        migrations.RunPython(flatten_json_ext, reverse_flatten_json_ext),
    ]
