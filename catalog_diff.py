#!/usr/bin/env python3
"""Compare published A2UI 1.0 catalog surfaces without resolving references."""
import argparse
import json
import sys
from pathlib import Path


def checked_obj(value, context):
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def strings(values, context):
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError(f"{context} must be an array of strings")
    return set(values)


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def reject_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


def check_schema(schema, context):
    if not isinstance(schema, (dict, bool)):
        raise ValueError(f"{context} must be an object or boolean schema")
    if isinstance(schema, dict):
        for name, child in checked_obj(schema.get("properties", {}), context).items():
            check_schema(child, f"{context}/{name}")
        strings(schema.get("required", []), context + "/required")


def catalog(value):
    value = checked_obj(value, "catalog")
    if value.get("protocolVersion") != "1.0":
        raise ValueError("only explicit catalog protocolVersion 1.0 is supported")
    identifier = value.get("catalogId")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("catalogId must be a non-empty string")
    for kind in ("components", "functions"):
        for name, definition in checked_obj(value.get(kind, {}), kind).items():
            context = f"{kind}/{name}"
            checked_obj(definition, context)
            check_schema(definition, context)
            if kind == "functions":
                args = definition.get("properties", {}).get("args", {})
                checked_obj(args, context + "/args")
                call = definition.get("properties", {}).get("@call")
                if (
                    definition.get("type") != "object"
                    or not isinstance(call, dict)
                    or not isinstance(call.get("const"), str)
                    or "@call" not in definition.get("required", [])
                    or definition.get("returnType") not in (
                        "string", "number", "boolean", "array", "object",
                        "validationResult", "any", "void"
                    )
                ):
                    raise ValueError(f"{context} is not a supported A2UI 1.0 function definition")
    return identifier, value


def load_export(path):
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        payload = checked_obj(payload, "export")
        if "catalogId" in payload:
            identifier, definition = catalog(payload)
            return "catalog", {identifier: definition}, set()
        wrapper = next(
            (k for k in ("a2uiRendererCapabilities", "rendererCapabilities") if k in payload),
            None,
        )
        if wrapper:
            payload = checked_obj(payload[wrapper], wrapper)
        if "1.0" not in payload:
            raise ValueError("expected a 1.0 catalog or version-keyed renderer capabilities")
        cap = checked_obj(payload["1.0"], "rendererCapabilities/1.0")
        supported = strings(cap.get("supportedCatalogIds"), "supportedCatalogIds")
        inline = cap.get("inlineCatalogs", [])
        if not isinstance(inline, list):
            raise ValueError("inlineCatalogs must be an array")
        catalogs = {}
        for item in inline:
            identifier, definition = catalog(item)
            if identifier in catalogs:
                raise ValueError(f"duplicate inline catalogId {identifier!r}")
            catalogs[identifier] = definition
        return "capabilities", catalogs, supported
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError(f"{path}: {error}") from error


def pointer(parts):
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def normalized(schema):
    """Canonicalize schema sets without changing order inside instance values."""
    if not isinstance(schema, dict):
        return schema
    result = schema.copy()
    schema_maps = ("properties", "patternProperties", "dependentSchemas", "$defs", "definitions")
    schema_arrays = ("allOf", "anyOf", "oneOf", "prefixItems")
    schema_nodes = (
        "additionalProperties", "unevaluatedProperties", "propertyNames", "items",
        "unevaluatedItems", "contains", "not", "if", "then", "else", "contentSchema",
    )
    if isinstance(result.get("required"), list) and all(isinstance(v, str) for v in result["required"]):
        result["required"] = sorted(set(result["required"]))
    if isinstance(result.get("enum"), list):
        result["enum"] = sorted(result["enum"], key=lambda member: json.dumps(member, sort_keys=True))
    for key in schema_maps:
        if isinstance(result.get(key), dict):
            result[key] = {name: normalized(child) for name, child in result[key].items()}
    for key in schema_arrays:
        if isinstance(result.get(key), list):
            result[key] = [normalized(child) for child in result[key]]
    for key in schema_nodes:
        if key in result:
            result[key] = normalized(result[key])
    return result


def same(a, b):
    # Canonical JSON avoids Python equating JSON booleans and numbers.
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def local_union(schema, kind, definitions):
    """Return exact reachable names, or None for unresolved/ambiguous shapes."""
    if not isinstance(schema, dict) or schema.keys() - {"oneOf", "discriminator"}:
        return None
    expected_discriminator = "component" if kind == "components" else "@call"
    if "discriminator" in schema and schema["discriminator"] != {"propertyName": expected_discriminator}:
        return None
    branches = schema.get("oneOf")
    if not isinstance(branches, list) or not branches:
        return None
    targets = {"#" + pointer((kind, name)): name for name in definitions}
    found = set()
    for branch in branches:
        if not isinstance(branch, dict) or branch.keys() != {"$ref"}:
            return None
        ref = branch["$ref"]
        if not isinstance(ref, str) or "%" in ref or ref not in targets or targets[ref] in found:
            return None
        found.add(targets[ref])
    return found


def catalog_defs(definition):
    defs = definition.get("$defs", {})
    return defs if isinstance(defs, dict) else {}
def compare(before, after):
    if before[0] != after[0]:
        raise ValueError("compare two catalogs or two capabilities exports, not a mix")
    changes = []

    def add(level, path, message):
        changes.append((level, pointer(path), message))

    def compare_keys(old, new, path, label, unavailable=()):
        for name in sorted(old.keys() - new.keys()):
            if name in unavailable:
                add("REVIEW", (*path, name), "inline definition no longer available; catalog is still advertised")
            else:
                add("BREAKING", (*path, name), f"{label} removed")
        for name in sorted(new.keys() - old.keys()):
            add("INFO", (*path, name), f"{label} added")
        return sorted(old.keys() & new.keys())

    def surface(old, new, path, label):
        for name in compare_keys(old.get("properties", {}), new.get("properties", {}),
                                 (*path, "properties"), label):
            a, b = (s["properties"][name] for s in (old, new))
            target = (*path, "properties", name)
            if isinstance(a, dict) and isinstance(b, dict):
                surface(a, b, target, label)
            elif not same(a, b):
                add("REVIEW", target, "schema changed; inspect compatibility")
        old_req, new_req = (set(s.get("required", [])) for s in (old, new))
        for name in sorted(new_req - old_req):
            add("BREAKING", (*path, "required", name), f"{label} became required")
        for name in sorted(old_req - new_req):
            add("INFO", (*path, "required", name), f"{label} no longer required")
        ignored = {"properties", "required"}
        rest = [
            normalized({k: v for k, v in s.items() if k not in ignored}) for s in (old, new)
        ]
        for key in sorted(rest[0].keys() | rest[1].keys()):
            if key not in rest[0] or key not in rest[1] or not same(rest[0][key], rest[1][key]):
                add("REVIEW", (*path, key), "schema changed; inspect compatibility")

    _, old_catalogs, old_supported = before
    _, new_catalogs, new_supported = after
    for name in sorted(old_supported - new_supported):
        add("BREAKING", ("supportedCatalogIds", name), "advertised catalog removed")
    for name in sorted(new_supported - old_supported):
        add("INFO", ("supportedCatalogIds", name), "advertised catalog added")
    for identifier in sorted((old_supported & new_supported) - (old_catalogs.keys() | new_catalogs.keys())):
        add("REVIEW", ("catalogs", identifier), "advertised only; definitions were not compared")
    for identifier in compare_keys(old_catalogs, new_catalogs, ("catalogs",), "catalog", new_supported):
        path = ("catalogs", identifier)
        old, new = old_catalogs[identifier], new_catalogs[identifier]
        old_defs, new_defs = catalog_defs(old), catalog_defs(new)
        for union, kind, label in (("anyComponent", "components", "component"),
                                   ("anyFunction", "functions", "function")):
            if union not in old_defs and union not in new_defs:
                continue
            a, b = (definition.get(union) for definition in (old_defs, new_defs))
            before_names = local_union(a, kind, old.get(kind, {}))
            after_names = local_union(b, kind, new.get(kind, {}))
            target = (*path, "$defs", union, "oneOf")
            if before_names is None or after_names is None:
                if (union in old_defs) != (union in new_defs) or not same(normalized(a), normalized(b)):
                    add("REVIEW", target, "cannot compare union reachability; unsupported or unresolved branches")
                continue
            for name in sorted(before_names - after_names):
                if name in new.get(kind, {}):
                    add("BREAKING", (*target, name), f"declared {label} no longer exposed by union")
            for name in sorted(after_names - before_names):
                if name in old.get(kind, {}):
                    add("INFO", (*target, name), f"declared {label} newly exposed by union")
            if a.get("discriminator") != b.get("discriminator"):
                add("REVIEW", (*path, "$defs", union, "discriminator"), "union discriminator changed")
        for key in sorted((old.keys() | new.keys()) - {"components", "functions"}):
            if key == "$defs" and all(isinstance(d.get(key, {}), dict) for d in (old, new)):
                other = [{name: schema for name, schema in defs.items()
                          if name not in {"anyComponent", "anyFunction"}} for defs in (old_defs, new_defs)]
                if same(*(normalized({"$defs": definition}) for definition in other)):
                    continue
            if key not in old or key not in new or not same(
                normalized({key: old[key]}), normalized({key: new[key]})
            ):
                add("REVIEW", (*path, key), "catalog metadata or definitions changed")
        for kind, label in (("components", "component"), ("functions", "function")):
            old_defs, new_defs = (s.get(kind, {}) for s in (old, new))
            for name in compare_keys(old_defs, new_defs, (*path, kind), label):
                target = (*path, kind, name)
                a, b = old_defs[name], new_defs[name]
                if kind == "components":
                    surface(a, b, target, "property")
                else:
                    aa, bb = (item["properties"].get("args", {}) for item in (a, b))
                    surface(aa, bb, (*target, "properties", "args"), "argument")
                    old_call, new_call = (item["properties"]["@call"] for item in (a, b))
                    if old_call["const"] != new_call["const"]:
                        add("BREAKING", (*target, "properties", "@call", "const"),
                            "function call name changed")
                    elif not same(old_call, new_call):
                        add("REVIEW", (*target, "properties", "@call"), "function call schema changed")
                    for key in sorted((a.keys() | b.keys()) - {"properties", "required"}):
                        if key not in a or key not in b or not same(a[key], b[key]):
                            add("REVIEW", (*target, key), "function metadata changed")
                    if ("args" in b["required"]) != ("args" in a["required"]):
                        required = "args" in b["required"]
                        add("BREAKING" if required else "INFO", (*target, "required", "args"),
                            "function args became required" if required else "function args no longer required")
    return sorted(changes, key=lambda item: (item[1], item[0], item[2]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    try:
        changes = compare(load_export(args.before), load_export(args.after))
    except (ValueError, RecursionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for level, path, message in changes:
        safe_path = json.dumps(path, ensure_ascii=True)[1:-1]
        print(f"{level} {safe_path}: {message}")
    if not changes:
        print("No changes detected.")
    return int(any(level == "BREAKING" for level, _, _ in changes))


if __name__ == "__main__":
    sys.exit(main())
