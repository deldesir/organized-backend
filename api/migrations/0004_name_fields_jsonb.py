"""Name fields varchar → jsonb, with data repair.

The CharField columns silently str()'d assigned dicts into Python-repr
strings ("{'value': 'Blondel', ...}"), which every backup sync cycle then
re-wrapped one level deeper. This migration unwraps whatever historical
shape each row holds down to the bare name, writes canonical
{"value": ..., "updatedAt": ""} JSON text, and converts the columns.
"""

import ast
import json

from django.db import migrations, models


def _bare_name(raw, depth=0):
    while depth < 8:
        if isinstance(raw, dict):
            raw = raw.get("value", "")
        elif isinstance(raw, str) and raw.strip().startswith(("{'value'", '{"value"')):
            try:
                raw = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    break
        else:
            break
        depth += 1
    return str(raw or "")


def repair_names(apps, schema_editor):
    CongUser = apps.get_model("api", "CongUser")
    for cu in CongUser.objects.all():
        cu.firstname = json.dumps(
            {"value": _bare_name(cu.firstname), "updatedAt": ""}
        )
        cu.lastname = json.dumps(
            {"value": _bare_name(cu.lastname), "updatedAt": ""}
        )
        cu.save(update_fields=["firstname", "lastname"])


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0003_congbackuptable_metadata_userbackuptable"),
    ]

    operations = [
        # 1. Repair while the columns are still text (writes valid JSON text).
        migrations.RunPython(repair_names, migrations.RunPython.noop),
        # 2. Convert the columns; the USING cast now succeeds.
        migrations.RunSQL(
            sql=(
                "ALTER TABLE organized_cong_user "
                "ALTER COLUMN firstname TYPE jsonb USING firstname::jsonb, "
                "ALTER COLUMN lastname TYPE jsonb USING lastname::jsonb;"
            ),
            reverse_sql=(
                "ALTER TABLE organized_cong_user "
                "ALTER COLUMN firstname TYPE varchar(100) USING firstname::text, "
                "ALTER COLUMN lastname TYPE varchar(100) USING lastname::text;"
            ),
            state_operations=[
                migrations.AlterField(
                    model_name="conguser",
                    name="firstname",
                    field=models.JSONField(default=dict),
                ),
                migrations.AlterField(
                    model_name="conguser",
                    name="lastname",
                    field=models.JSONField(default=dict),
                ),
            ],
        ),
    ]
