"""Re-clean firstname/lastname to the canonical {value, updatedAt} shape.

Migration 0004 converted the columns to jsonb and unwrapped them once, but a
stale client's Dexie re-uploaded nested/repr values through the backup save
path afterwards (which stored whatever it received), re-poisoning some rows.
The application code now normalizes every write through
`api.services.names.name_field`, so this one-time pass makes existing rows
canonical again; nothing can re-nest them after this.
"""

from django.db import migrations

from api.services.names import name_field


def reclean(apps, schema_editor):
    CongUser = apps.get_model("api", "CongUser")
    for cu in CongUser.objects.all():
        cu.firstname = name_field(cu.firstname)
        cu.lastname = name_field(cu.lastname)
        cu.save(update_fields=["firstname", "lastname"])


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0004_name_fields_jsonb"),
    ]

    operations = [
        migrations.RunPython(reclean, migrations.RunPython.noop),
    ]
