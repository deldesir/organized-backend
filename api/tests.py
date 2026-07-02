from django.test import TestCase

from api.services.names import bare_name, name_field


class NameCanonicalizationTests(TestCase):
    """Lock in the single canonical shape for firstname/lastname.

    Regression guard for the cascade where CharField columns str()'d dicts and
    endpoints disagreed on wrap depth, nesting names one level deeper each
    login+backup cycle.
    """

    def test_bare_name_plain_string(self):
        self.assertEqual(bare_name("Blondel"), "Blondel")

    def test_bare_name_single_dict(self):
        self.assertEqual(bare_name({"value": "Blondel", "updatedAt": ""}), "Blondel")

    def test_bare_name_python_repr_string(self):
        self.assertEqual(bare_name("{'value': 'Blondel', 'updatedAt': ''}"), "Blondel")

    def test_bare_name_triple_nested(self):
        poison = {
            "value": "{'value': \"{'value': 'Blondel', 'updatedAt': ''}\", 'updatedAt': ''}",
            "updatedAt": "",
        }
        self.assertEqual(bare_name(poison), "Blondel")

    def test_bare_name_empty_and_none(self):
        self.assertEqual(bare_name(None), "")
        self.assertEqual(bare_name(""), "")
        self.assertEqual(bare_name({}), "")

    def test_name_field_shape(self):
        self.assertEqual(
            name_field("Blondel"), {"value": "Blondel", "updatedAt": ""}
        )

    def test_name_field_preserves_updated_at(self):
        self.assertEqual(
            name_field({"value": "Blondel", "updatedAt": "2026-07-02"}),
            {"value": "Blondel", "updatedAt": "2026-07-02"},
        )

    def test_name_field_is_idempotent(self):
        once = name_field("Blondel")
        twice = name_field(once)
        thrice = name_field(twice)
        self.assertEqual(once, twice)
        self.assertEqual(twice, thrice)
        self.assertEqual(thrice["value"], "Blondel")
