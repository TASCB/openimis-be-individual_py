from django.db import migrations


def add_pmt_right(apps, schema_editor):
    RoleRight = apps.get_model('core', 'RoleRight')
    Role = apps.get_model('core', 'Role')

    role = Role.objects.get(is_system=64)

    if not RoleRight.objects.filter(
        validity_to__isnull=True,
        role=role,
        right_id=180005
    ).exists():
        RoleRight.objects.create(
            role=role,
            right_id=180005,
            audit_user_id=1
        )
        print("✓ Permission 180005 added to admin role")
    else:
        print("✓ Permission 180005 already exists")

    # Explicitly clear cache since apps.get_model() bypasses
    # ORM signals in migration context
    try:
        from django.core.cache import cache
        cache.clear()
        print("✓ Cache cleared")
    except Exception as e:
        print(f"Warning: Could not clear cache: {e}")


def remove_pmt_right(apps, schema_editor):
    RoleRight = apps.get_model('core', 'RoleRight')
    RoleRight.objects.filter(
        role__is_system=64,
        right_id=180005,
        validity_to__isnull=True
    ).delete()

    try:
        from django.core.cache import cache
        cache.clear()
    except Exception as e:
        print(f"Warning: Could not clear cache: {e}")


class Migration(migrations.Migration):
    dependencies = [
        ('individual', '0019_flatten_json_ext')
    ]

    operations = [
        migrations.RunPython(add_pmt_right, remove_pmt_right),
    ]