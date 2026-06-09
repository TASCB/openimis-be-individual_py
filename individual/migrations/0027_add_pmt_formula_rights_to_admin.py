from django.db import migrations

PMT_FORMULA_RIGHTS = [180006, 180007]


def add_pmt_formula_rights(apps, schema_editor):
    RoleRight = apps.get_model('core', 'RoleRight')
    Role = apps.get_model('core', 'Role')

    role = Role.objects.get(is_system=64)

    for right_id in PMT_FORMULA_RIGHTS:
        if not RoleRight.objects.filter(
            validity_to__isnull=True,
            role=role,
            right_id=right_id,
        ).exists():
            RoleRight.objects.create(
                role=role,
                right_id=right_id,
                audit_user_id=1,
            )
            print(f"✓ Permission {right_id} added to admin role")
        else:
            print(f"✓ Permission {right_id} already exists")

    try:
        from django.core.cache import cache
        cache.clear()
        print("✓ Cache cleared")
    except Exception as e:
        print(f"Warning: Could not clear cache: {e}")


def remove_pmt_formula_rights(apps, schema_editor):
    RoleRight = apps.get_model('core', 'RoleRight')
    RoleRight.objects.filter(
        role__is_system=64,
        right_id__in=PMT_FORMULA_RIGHTS,
        validity_to__isnull=True,
    ).delete()

    try:
        from django.core.cache import cache
        cache.clear()
    except Exception as e:
        print(f"Warning: Could not clear cache: {e}")


class Migration(migrations.Migration):
    dependencies = [
        ('individual', '0026_pmt_global_formula'),
    ]

    operations = [
        migrations.RunPython(add_pmt_formula_rights, remove_pmt_formula_rights),
    ]
