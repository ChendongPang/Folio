"""Wire-schema compatibility for strict structured output."""

from copy import deepcopy


def strict_output_schema(config, schema):
    """Require explicit fields without changing canonical schemas or nullable types."""
    if config.get("structured_output") != "json_schema":
        return schema

    schema = deepcopy(schema)

    def adapt(node):
        if not isinstance(node, dict):
            return
        # Defaults are annotations, not a license to omit strict output fields.
        # Keep defaults on canonical models; do not send them to this provider.
        node.pop("default", None)
        properties = node.get("properties")
        if isinstance(properties, dict):
            node["required"] = list(dict.fromkeys([*node.get("required", []), *properties]))
        # Traverse schema locations only: const/enum/examples may contain data
        # objects whose keys happen to be named properties, required or default.
        for key in ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas"):
            children = node.get(key)
            if isinstance(children, dict):
                for child in children.values():
                    adapt(child)
        for key in (
            "items",
            "additionalProperties",
            "contains",
            "propertyNames",
            "not",
            "if",
            "then",
            "else",
            "unevaluatedProperties",
            "unevaluatedItems",
        ):
            adapt(node.get(key))
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    adapt(child)

    adapt(schema)
    return schema


STRICT_FIELDS_INSTRUCTION = (
    "\nThis endpoint uses strict JSON Schema. Explicitly return every required field in every object, even"
    " if examples omit it; the current Schema governs. Inapplicable values must satisfy the field's "
    "declared type and constraints. Use null only where allowed, and [] or an empty string only where "
    "permitted and appropriate. Never invent sources or business content."
)
