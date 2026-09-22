import inspect
from typing import Any, Callable, get_type_hints

from pydantic import create_model

TOOLS: list[dict] = []
TOOL_FUNCTIONS: dict[str, Callable] = {}


def tool(fn: Callable) -> Callable:
    """Register fn as an agent tool, building its OpenAI-style schema from
    its type hints and docstring."""
    signature = inspect.signature(fn)
    type_hints = get_type_hints(fn)

    fields = {}
    for name, param in signature.parameters.items():
        annotation = type_hints.get(name, Any)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (annotation, default)

    params_model = create_model(f"{fn.__name__}_params", **fields)
    parameters_schema = params_model.model_json_schema()
    parameters_schema.pop("title", None)
    for prop in parameters_schema.get("properties", {}).values():
        prop.pop("title", None)

    TOOLS.append({
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": inspect.getdoc(fn) or "",
            "parameters": parameters_schema,
        },
    })
    TOOL_FUNCTIONS[fn.__name__] = fn

    return fn
