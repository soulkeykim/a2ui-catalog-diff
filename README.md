# A2UI Catalog Diff

Small, offline CLI for spotting breaking changes in A2UI 1.0 catalog surfaces before shipping a renderer. Python 3.9+; no dependencies.

```sh
python3 catalog_diff.py previous.json candidate.json
```

Compare two standalone catalog definitions (each with `catalogId` and explicit `protocolVersion: "1.0"`) or two version-keyed renderer capabilities exports. The latter can be the bare `{"1.0": ...}` object or use an `a2uiRendererCapabilities` / `rendererCapabilities` wrapper. Other capability versions are out of scope. Catalogs are matched by ID, not array position.

| Code | Meaning |
| --- | --- |
| 0 | No breaking change detected. Review any `REVIEW` lines. |
| 1 | At least one breaking change detected. |
| 2 | Input could not be read, did not match a supported shape, or two different export types were supplied. |

The CLI flags removed catalogs, components, functions and explicit properties/arguments, plus new required fields/arguments. A declared component or function removed from a directly comparable `$defs.anyComponent.oneOf` or `$defs.anyFunction.oneOf` is also breaking. These unions are compared only when both sides consist exclusively of distinct, exact local JSON Pointer references to their respective declaration maps, with at most the standard discriminator alongside `oneOf`; changes involving an unknown shape produce a `REVIEW` line. An optional property or definition addition is informational. Changes to other keywords or metadata are marked for review. An inline catalog still advertised by ID is not marked removed if its inline definition disappears; its contents cannot be compared. An advertised ID removed from capabilities is reported independently from inline definitions. JSON object order and schema `required` and `enum` array order do not affect the result.

This is a structural guardrail, not full A2UI or JSON Schema validation, a semantic compatibility proof, or an exhaustive schema diff. Only direct `properties` trees are compared field by field. Except for those two supported local unions, composition (`allOf`, `anyOf`, `oneOf`), references, rules under arrays, constraints and metadata are inspected as opaque changes requiring review; nothing is resolved or fetched. Moving a field into a composition may appear as a removal. Changes behind paths whose references do not change cannot be detected unless reflected in compared local definitions. Do not interpret exit 0 as a compatibility guarantee.

## Local example and CI

The synthetic example reproduces the shape of a Swift inline export regression: `Button.child` and `Button.action` disappear while the Checkable trait remains; it also includes a separately demonstrated required function argument.

```sh
python3 catalog_diff.py examples/before.json examples/does-not-exist.json  # 2
python3 catalog_diff.py examples/before.json examples/additive.json         # 0
python3 catalog_diff.py examples/before.json examples/breaking.json         # 1
python3 -m unittest -v
```

In CI, export the base and candidate files using the same renderer generator, then run a normal shell step:

```sh
python3 catalog_diff.py "$BASE_CATALOG_JSON" "$CANDIDATE_CATALOG_JSON"
```

Keep the command's exit status so CI fails on detected breakage or invalid input. Review warnings separately.
