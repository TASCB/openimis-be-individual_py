from django.db import migrations


def flatten_nested_json_ext(apps, schema_editor):
    Individual = apps.get_model("individual", "Individual")
    Group = apps.get_model("individual", "Group")

    def flatten_model(model):
        for instance in model.objects.filter(json_ext__has_key="json_ext").iterator():
            json_ext = instance.json_ext or {}
            nested = json_ext.get("json_ext")
            if not isinstance(nested, dict):
                continue

            flattened = dict(json_ext)
            flattened.pop("json_ext", None)
            for key, value in nested.items():
                flattened.setdefault(key, value)

            if flattened != json_ext:
                instance.json_ext = flattened
                instance.save(update_fields=["json_ext"])

    flatten_model(Individual)
    flatten_model(Group)


class Migration(migrations.Migration):

    dependencies = [
        ("individual", "0023_add_consent_index"),
    ]

    operations = [
        migrations.RunPython(flatten_nested_json_ext, migrations.RunPython.noop),
    ]
