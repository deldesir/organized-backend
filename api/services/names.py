"""Canonical handling of the {value, updatedAt} name fields.

The frontend models firstname/lastname as {value, updatedAt} objects. Two
historical mistakes poisoned these fields:

  1. The columns were CharField, so an assigned dict was silently str()'d into
     a Python-repr string ("{'value': 'Blondel', 'updatedAt': ''}").
  2. Endpoints disagreed on the shape — some stored bare strings, some stored
     the client dict; login unwrapped exactly one level; the admin serializer
     wrapped unconditionally. Every login+backup cycle therefore nested one
     level deeper ("{'value': \"{'value': 'Blondel'...}\"}").

These helpers are the SINGLE source of truth. `bare_name` deep-unwraps any
historical shape (dict, repr-string, JSON-string, arbitrarily nested) down to
the plain string; `name_field` produces the canonical {value, updatedAt} dict
for storage and serialization. Every read and write of firstname/lastname must
go through them, so no shape can re-enter the system and no cycle can re-nest.
"""

import ast
import json

_MAX_DEPTH = 12


def bare_name(raw, _depth=0):
    """Reduce any historical name shape to its plain string value."""
    while _depth < _MAX_DEPTH:
        if isinstance(raw, dict):
            raw = raw.get('value', '')
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
        _depth += 1
    if raw is None:
        return ''
    return str(raw)


def name_field(raw, updated_at=''):
    """Canonical {value, updatedAt} dict, deep-unwrapping any historical shape.

    Preserves a top-level updatedAt already carried by the incoming value (so a
    client's edit timestamp is not lost); otherwise uses the provided default.
    """
    if isinstance(raw, dict) and raw.get('updatedAt'):
        updated_at = raw.get('updatedAt')
    return {'value': bare_name(raw), 'updatedAt': updated_at or ''}
