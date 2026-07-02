from django.test import TestCase

from api.services.names import bare_name, name_field


class CountriesDataTests(TestCase):
    """The public countries endpoint must offer worldwide coverage."""

    def test_full_iso_list(self):
        from api.services.countries import COUNTRIES
        codes = {c for c, _ in COUNTRIES}
        self.assertGreater(len(COUNTRIES), 150)
        # The two previously-hardcoded ones must still be present.
        self.assertIn("US", codes)
        self.assertIn("HT", codes)
        # A sampling of every-continent coverage.
        for c in ("FR", "JP", "BR", "NG", "AU", "CA"):
            self.assertIn(c, codes)

    def test_codes_are_unique_alpha2(self):
        from api.services.countries import COUNTRIES
        codes = [c for c, _ in COUNTRIES]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertTrue(all(len(c) == 2 for c in codes))


class WebhookPhoneMatchTests(TestCase):
    """Phone normalization for assignment-response person resolution."""

    def test_digits_strips_formatting(self):
        from api.views.webhooks import _digits
        self.assertEqual(_digits("+509 3712-3456"), "50937123456")
        self.assertEqual(_digits(None), "")


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
