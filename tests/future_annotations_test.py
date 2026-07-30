"""Regression tests for TypedDict Required/NotRequired under postponed evaluation.

`from __future__ import annotations` (PEP 563) turns every annotation in this module
into a lazy string. Python's own TypedDict metaclass computes `__required_keys__`/
`__optional_keys__` by inspecting the *raw* annotation object at class-creation time,
so under postponed evaluation it never sees a `Required[...]`/`NotRequired[...]`
wrapper - it only sees an unevaluated string/ForwardRef - and silently falls back to
the class's `total` default. `McpServer._typed_dict_to_schema` corrects for this by
re-checking each field's fully-resolved type (via `get_type_hints`, which always
resolves forward refs) and overriding the metaclass's bucket when it finds an explicit
qualifier the metaclass missed.

This file MUST keep the future-annotations import at the top - that is the entire
point of these tests. Every TypedDict/function below is deliberately defined at
module level so its annotations really do go through postponed evaluation.
"""
from __future__ import annotations

from typing import NotRequired, Required, TypedDict

from zeromcp import McpServer


def _list_tool_schema(server: McpServer, tool_name: str) -> dict:
    response = server._dispatch_mcp({
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1,
    })
    assert response is not None
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    return tools[tool_name]


class BaseResult(TypedDict):
    instances: list


class ResultWithHint(BaseResult):
    hint: NotRequired[str]


def test_notrequired_field_on_subclassed_typeddict_is_optional():
    print("Testing NotRequired field inherited across a TypedDict subclass...")
    # The metaclass itself gets this wrong under postponed evaluation - guard the
    # actual bug, not just the schema zeromcp derives from it, so this test still
    # means something if a future CPython fixes TypedDict for this case.
    assert "hint" in ResultWithHint.__required_keys__ and "hint" not in ResultWithHint.__optional_keys__, (
        "expected the underlying CPython bug to still misclassify 'hint' as required; "
        "if this now fails, __required_keys__/__optional_keys__ may no longer need the override below"
    )

    server = McpServer("future-annotations-notrequired")

    @server.tool
    def list_databases() -> ResultWithHint:
        return {"instances": []}

    schema = _list_tool_schema(server, "list_databases")["outputSchema"]
    assert schema["required"] == ["instances"], schema["required"]
    assert set(schema["properties"]) == {"instances", "hint"}
    print("✓ PASS")


class TotalFalseWithRequired(TypedDict, total=False):
    optional_field: str
    required_field: Required[str]


def test_required_field_on_total_false_typeddict_is_required():
    print("Testing Required field on a total=False TypedDict...")
    server = McpServer("future-annotations-required")

    @server.tool
    def make() -> TotalFalseWithRequired:
        return {"required_field": "x"}

    schema = _list_tool_schema(server, "make")["outputSchema"]
    assert schema["required"] == ["required_field"], schema["required"]
    assert set(schema["properties"]) == {"optional_field", "required_field"}
    assert schema["properties"]["required_field"] == {"type": "string"}
    print("✓ PASS")


class PlainTotalTrue(TypedDict):
    a: str
    b: int


def test_plain_typeddict_without_qualifiers_stays_total_true():
    print("Testing an unqualified TypedDict is unaffected by the override...")
    server = McpServer("future-annotations-plain")

    @server.tool
    def make() -> PlainTotalTrue:
        return {"a": "x", "b": 1}

    schema = _list_tool_schema(server, "make")["outputSchema"]
    assert sorted(schema["required"]) == ["a", "b"], schema["required"]
    print("✓ PASS")


class PlainTotalFalse(TypedDict, total=False):
    a: str
    b: int


def test_plain_total_false_typeddict_has_no_required_fields():
    print("Testing a total=False TypedDict with no explicit qualifiers stays optional...")
    server = McpServer("future-annotations-total-false")

    @server.tool
    def make() -> PlainTotalFalse:
        return {}

    schema = _list_tool_schema(server, "make")["outputSchema"]
    assert schema["required"] == [], schema["required"]
    print("✓ PASS")


class InputWithHint(TypedDict):
    path: str
    hint: NotRequired[str]


def test_notrequired_field_as_input_parameter():
    print("Testing NotRequired field on a TypedDict used as a tool parameter...")
    server = McpServer("future-annotations-input")

    @server.tool
    def open_thing(options: InputWithHint) -> None:
        pass

    schema = _list_tool_schema(server, "open_thing")["inputSchema"]
    options_schema = schema["properties"]["options"]
    assert options_schema["required"] == ["path"], options_schema["required"]
    assert set(options_schema["properties"]) == {"path", "hint"}
    print("✓ PASS")


class InnerWithHint(TypedDict):
    name: str
    hint: NotRequired[str]


class OuterList(TypedDict):
    items: list[InnerWithHint]


def test_notrequired_field_in_nested_typeddict():
    print("Testing NotRequired field on a TypedDict nested inside another TypedDict...")
    server = McpServer("future-annotations-nested")

    @server.tool
    def list_items() -> OuterList:
        return {"items": []}

    schema = _list_tool_schema(server, "list_items")["outputSchema"]
    inner_schema = schema["properties"]["items"]["items"]
    assert inner_schema["required"] == ["name"], inner_schema["required"]
    assert set(inner_schema["properties"]) == {"name", "hint"}
    print("✓ PASS")


_lazy_registration_server = McpServer("future-annotations-lazy-registration")


# `get_type_hints` resolves forward refs against `func.__globals__`, so the
# referenced type has to be module-level, not local to a test function, for this
# to actually exercise the timing this test cares about: LaterDefined does not
# exist yet when `@server.tool` runs. If @tool ever goes back to eagerly building
# the schema at decoration time, this raises NameError right here instead of
# resolving once the module finishes loading - breaking a legitimate pattern.
@_lazy_registration_server.tool
def make_later_defined() -> "LaterDefined":
    return {"value": 1}


class LaterDefined(TypedDict):
    value: int


def test_tool_registration_does_not_eagerly_resolve_forward_references():
    print("Testing @tool does not eagerly resolve same-module forward references...")
    schema = _list_tool_schema(_lazy_registration_server, "make_later_defined")["outputSchema"]
    assert schema["required"] == ["value"], schema["required"]
    print("✓ PASS")


def run_all_tests():
    test_notrequired_field_on_subclassed_typeddict_is_optional()
    test_required_field_on_total_false_typeddict_is_required()
    test_plain_typeddict_without_qualifiers_stays_total_true()
    test_plain_total_false_typeddict_has_no_required_fields()
    test_notrequired_field_as_input_parameter()
    test_notrequired_field_in_nested_typeddict()
    test_tool_registration_does_not_eagerly_resolve_forward_references()
    print("\n" + "=" * 60)
    print("ALL FUTURE-ANNOTATIONS TESTS PASSED! ✓")
    print("=" * 60)


if __name__ == "__main__":
    run_all_tests()
