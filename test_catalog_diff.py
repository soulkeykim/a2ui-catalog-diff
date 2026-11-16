import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from catalog_diff import compare, load_export

ROOT = Path(__file__).parent
EXAMPLES = ROOT / "examples"


class CatalogDiffTests(unittest.TestCase):
    def setUp(self):
        self.before = load_export(EXAMPLES / "before.json")

    def test_detects_button_field_loss_and_required_argument(self):
        changes = compare(self.before, load_export(EXAMPLES / "breaking.json"))
        self.assertEqual([level for level, _, _ in changes], ["BREAKING"] * 3)
        self.assertIn("/components/Button/properties/action", changes[0][1])
        self.assertIn("/components/Button/properties/child", changes[1][1])
        self.assertIn("/functions/formatLabel/properties/args/required/label", changes[2][1])

    def test_new_optional_property(self):
        changes = compare(self.before, load_export(EXAMPLES / "additive.json"))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0][0], "INFO")

    def test_required_and_object_key_order(self):
        old = copy.deepcopy(self.before)
        old_button = next(iter(old[1].values()))["components"]["Button"]
        old_button["required"] = ["component", "action"]
        old_button["allOf"].append({"required": ["component", "child"]})
        new = copy.deepcopy(old)
        new_button = next(iter(new[1].values()))["components"]["Button"]
        new_button["required"].reverse()
        new_button["allOf"][1]["required"].reverse()
        new_button["properties"] = dict(reversed(list(new_button["properties"].items())))
        self.assertEqual(compare(old, new), [])

    def test_opaque_composition_change_is_review_only(self):
        new = copy.deepcopy(self.before)
        next(iter(new[1].values()))["components"]["Button"]["allOf"] = [
            {"$ref": "https://never-fetch.invalid/other"}
        ]
        self.assertEqual([level for level, _, _ in compare(self.before, new)], ["REVIEW"])

    def test_catalog_still_advertised_without_inline_definition(self):
        identifier = next(iter(self.before[1]))
        new = ("capabilities", {}, {identifier})
        old = ("capabilities", self.before[1], {identifier})
        self.assertEqual([level for level, _, _ in compare(old, new)], ["REVIEW"])
        self.assertEqual([level for level, _, _ in compare(new, new)], ["REVIEW"])
        self.assertEqual([level for level, _, _ in compare(new, ("capabilities", {}, set()))],
                         ["BREAKING"])

    def test_remove_catalog_component_and_function(self):
        new = copy.deepcopy(self.before)
        definition = next(iter(new[1].values()))
        definition["components"].clear()
        definition["functions"].clear()
        self.assertEqual([level for level, _, _ in compare(self.before, new)], ["BREAKING"] * 2)
        self.assertEqual([level for level, _, _ in compare(self.before, ("capabilities", {}, set()))],
                         ["BREAKING"])

    def test_standalone_catalog_and_bad_inputs(self):
        identifier, catalog = next(iter(self.before[1].items()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "single.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            self.assertEqual(load_export(path), ("catalog", {identifier: catalog}, set()))
            with self.assertRaisesRegex(ValueError, "not a mix"):
                compare(self.before, load_export(path))
            for input_text in (
                '{', '{"catalogId":"test"}',
                '{"catalogId":"test","protocolVersion":"0.9"}',
                '{"1.0":{}}', '{"1.0":{"supportedCatalogIds":[],"inlineCatalogs":{}}}',
                '{"1.0":{"supportedCatalogIds":[],"supportedCatalogIds":[]}}',
                '{"catalogId":"test","protocolVersion":"1.0","components":{"A":{"required":42}}}',
            ):
                path.write_text(input_text, encoding="utf-8")
                with self.subTest(input_text=input_text), self.assertRaises(ValueError):
                    load_export(path)

    def test_exact_union_reachability_for_components_and_functions(self):
        old = copy.deepcopy(self.before)
        catalog = next(iter(old[1].values()))
        for kind, union, name in (("components", "anyComponent", "Button"),
                                  ("functions", "anyFunction", "formatLabel")):
            catalog[kind]["Name/with~escape"] = copy.deepcopy(catalog[kind][name])
            catalog.setdefault("$defs", {})[union] = {
                "oneOf": [{"$ref": f"#/{kind}/{name}"}, {"$ref": f"#/{kind}/Name~1with~0escape"}]
            }
        new = copy.deepcopy(old)
        defs = next(iter(new[1].values()))["$defs"]
        for definition in defs.values():
            definition["oneOf"].reverse()
        self.assertEqual(compare(old, new), [])
        for definition in defs.values():
            definition["oneOf"].pop()
        changes = compare(old, new)
        self.assertEqual([level for level, _, _ in changes], ["BREAKING", "BREAKING"])
        self.assertIn("/$defs/anyComponent/oneOf/Button", changes[0][1])
        self.assertIn("/$defs/anyFunction/oneOf/formatLabel", changes[1][1])

    def test_unknown_and_malformed_union_branches_are_review_only(self):
        old = copy.deepcopy(self.before)
        catalog = next(iter(old[1].values()))
        catalog["$defs"] = {"anyComponent": {"oneOf": [{"$ref": "#/components/Button"}]}}
        for shape in (
            {"oneOf": []},
            {"oneOf": "not an array"},
            {"oneOf": [{"$ref": "#/components/no-such-component"}]},
            {"oneOf": [{"$ref": "#/components/%42utton"}]},
            {"oneOf": [{"$ref": "https://example.test/schema"}]},
            {"oneOf": [{"$ref": "#/components/Button", "not": {}}]},
            {"oneOf": [{"$ref": "#/components/Button"}] * 2},
            {"oneOf": [{"$ref": "#/components/Button"}], "$id": "new-base.json"},
            None,
        ):
            with self.subTest(shape=shape):
                new = copy.deepcopy(old)
                next(iter(new[1].values()))["$defs"]["anyComponent"] = shape
                self.assertEqual([level for level, _, _ in compare(old, new)], ["REVIEW"])

    def test_enum_order_within_schema_and_ordered_instance_arrays(self):
        old = copy.deepcopy(self.before)
        props = next(iter(old[1].values()))["components"]["Button"]["properties"]
        props["action"]["enum"] = [{"enum": ["a", "b"]}, [1, 2], True]
        new = copy.deepcopy(old)
        members = next(iter(new[1].values()))["components"]["Button"]["properties"]["action"]["enum"]
        members.reverse()
        self.assertEqual(compare(old, new), [])
        members[1].reverse()
        self.assertEqual([level for level, _, _ in compare(old, new)], ["REVIEW"])
        members[1].reverse()
        members[2]["enum"].reverse()
        self.assertEqual([level for level, _, _ in compare(old, new)], ["REVIEW"])

    def test_cli_exit_codes(self):
        before = EXAMPLES / "before.json"
        for filename, exit_code in (("before.json", 0), ("additive.json", 0), ("breaking.json", 1),
                                    ("does-not-exist.json", 2)):
            run = subprocess.run(
                [sys.executable, str(ROOT / "catalog_diff.py"), str(before), str(EXAMPLES / filename)],
                capture_output=True, text=True,
            )
            self.assertEqual(run.returncode, exit_code, run.stderr)
            if exit_code == 2:
                self.assertIn("error:", run.stderr)


if __name__ == "__main__":
    unittest.main()
