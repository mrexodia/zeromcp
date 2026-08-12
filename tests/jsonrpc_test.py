"""
Comprehensive JSON-RPC 2.0 test suite for MCP implementation
"""
import asyncio
import json
import sys
import traceback
import re
from typing import Optional, Any, TypedDict

from zeromcp.jsonrpc import JsonRpcRegistry

# Create registry and register test methods
jsonrpc = JsonRpcRegistry()

class Point(TypedDict):
    x: int
    y: int

@jsonrpc.method
def subtract(minuend: int, subtrahend: int) -> int:
    return minuend - subtrahend

@jsonrpc.method
def update(a: int, b: int, c: int, d: int, e: int) -> str:
    return "updated"

@jsonrpc.method
def foobar() -> str:
    return "bar"

@jsonrpc.method
def get_data() -> list:
    return ["hello", 5]

@jsonrpc.method
def greet(name: str, greeting: str = "Hello") -> str:
    return f"{greeting}, {name}!"

@jsonrpc.method
def process_optional(value: Optional[int]) -> str:
    return f"Got: {value}"

@jsonrpc.method
def union_test(id: int | str | None | Point) -> str:
    return f"ID: {id or '<nil>'}"

@jsonrpc.method
def exact_union_test(value: float | bool | int) -> str:
    return f"{type(value).__name__}:{value!r}"

@jsonrpc.method
def list_test(items: list[str]) -> int:
    return len(items)

@jsonrpc.method
def exception():
    raise Exception("Python exception")

@jsonrpc.method
def point_pretty(p: Point) -> str:
    return f"Point(x={p['x']}, y={p['y']})"

@jsonrpc.method
def round_float(value: float) -> int:
    return round(value)

@jsonrpc.method
def python_repr(value: Any) -> str:
    return repr(value)

@jsonrpc.method
def unknown(x, y):
    return x + y

@jsonrpc.method
async def async_foobar() -> str:
    await asyncio.sleep(0)
    return f"async:{jsonrpc.current_request_id()}"

def matches_response(actual: dict | None, expected: dict | None) -> bool:
    """Check if actual response matches expected, with regex support for error messages."""
    if actual is None and expected is None:
        return True
    if actual is None or expected is None:
        return False

    # Check top-level keys
    if set(actual.keys()) != set(expected.keys()):
        return False

    for key in expected.keys():
        actual_val = actual[key]
        expected_val = expected[key]

        # Handle error object specially for regex matching
        if key == "error" and isinstance(expected_val, dict) and isinstance(actual_val, dict):
            # Check code exactly
            if actual_val.get("code") != expected_val.get("code"):
                return False

            # Check message with regex support
            expected_msg = expected_val.get("message", "")
            actual_msg = actual_val.get("message", "")
            if isinstance(expected_msg, str) and expected_msg.startswith("regex:"):
                pattern = expected_msg[6:]  # Remove "regex:" prefix
                if not re.search(pattern, actual_msg):
                    return False
            else:
                if actual_msg != expected_msg:
                    return False

            # For data field, support regex patterns
            if "data" in expected_val:
                expected_data = expected_val["data"]
                actual_data = actual_val.get("data", "")

                # If expected_data starts with "regex:", treat it as a regex pattern
                if isinstance(expected_data, str) and expected_data.startswith("regex:"):
                    pattern = expected_data[6:]  # Remove "regex:" prefix
                    if not re.search(pattern, str(actual_data)):
                        return False
                else:
                    # Exact match
                    if actual_data != expected_data:
                        return False
            elif "data" in actual_val:
                # Actual has data but expected doesn't - that's ok
                pass
        else:
            # Exact match for other fields
            if actual_val != expected_val:
                return False

    return True

def check_rpc(request: Any, expected_response: dict | None = None, description: str = ""):
    """Helper to test RPC calls"""
    print(f"\n{'='*60}")
    print(f"Test: {description}")
    print(f"--> {request}")

    try:
        result = jsonrpc.dispatch(request)
    except Exception:
        print("\n❌ UNEXPECTED EXCEPTION:")
        traceback.print_exc()
        sys.exit(1)

    if result is None:
        print("<-- (no response - notification)")
        if expected_response is not None:
            print("\n❌ FAIL: Expected response but got None")
            print("Expected: {json.dumps(expected_response, indent=2)}")
            sys.exit(1)
    else:
        result_json = json.dumps(result, indent=2)
        print(f"<-- {result_json}")

        if expected_response is not None:
            if not matches_response(result, expected_response): # type: ignore
                print("\n❌ FAIL: Response mismatch")
                print(f"Expected: {json.dumps(expected_response, indent=2)}")
                print(f"Got:      {result_json}")
                sys.exit(1)

    print("✓ PASS")
    return result


def check_async_method_dispatch():
    print(f"\n{'='*60}")
    print("Test: async method dispatch")

    result = jsonrpc.dispatch({"jsonrpc": "2.0", "method": "async_foobar", "id": "async-api-id"})
    expected = {"jsonrpc": "2.0", "result": "async:async-api-id", "id": "async-api-id"}
    if result != expected:
        print("❌ FAIL: dispatch returned wrong response")
        print(f"Expected: {expected}")
        print(f"Got: {result}")
        sys.exit(1)
    print("✓ PASS")


def check_current_request_id_is_stack_safe():
    print(f"\n{'='*60}")
    print("Test: current_request_id is restored across nested dispatch")

    registry = JsonRpcRegistry()
    seen = []

    @registry.method
    def inner():
        seen.append(("inner", registry.current_request_id()))
        return "ok"

    @registry.method
    async def outer():
        seen.append(("outer_before", registry.current_request_id()))
        response = await registry.dispatch_async({"jsonrpc": "2.0", "method": "inner", "id": "inner-id"})
        seen.append(("outer_after", registry.current_request_id()))
        assert response is not None
        return response["result"]

    result = registry.dispatch({"jsonrpc": "2.0", "method": "outer", "id": "outer-id"})
    expected_seen = [
        ("outer_before", "outer-id"),
        ("inner", "inner-id"),
        ("outer_after", "outer-id"),
    ]
    if result != {"jsonrpc": "2.0", "result": "ok", "id": "outer-id"} or seen != expected_seen or registry.current_request_id() is not None:
        print("❌ FAIL: current_request_id was not restored correctly")
        print(f"Result: {result}")
        print(f"Seen: {seen}")
        sys.exit(1)
    print("✓ PASS")


def run_all_tests():
    print("="*60)
    print("JSON-RPC 2.0 COMPLIANCE TESTS")
    print("="*60)

    # ========================================
    # SPEC EXAMPLES
    # ========================================

    # Positional parameters
    check_rpc(
        {"jsonrpc": "2.0", "method": "subtract", "params": [42, 23], "id": 1},
        {"jsonrpc": "2.0", "result": 19, "id": 1},
        "Positional params - subtract(42, 23)"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": [23, 42], "id": 2}',
        {"jsonrpc": "2.0", "result": -19, "id": 2},
        "Positional params - subtract(23, 42)"
    )

    # Named parameters
    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": {"subtrahend": 23, "minuend": 42}, "id": 3}',
        {"jsonrpc": "2.0", "result": 19, "id": 3},
        "Named params - order independent (1)"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": {"minuend": 42, "subtrahend": 23}, "id": 4}',
        {"jsonrpc": "2.0", "result": 19, "id": 4},
        "Named params - order independent (2)"
    )

    # Notifications (no response)
    check_rpc(
        '{"jsonrpc": "2.0", "method": "update", "params": [1,2,3,4,5]}',
        None,
        "Notification - update (no id)"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "foobar"}',
        None,
        "Notification - foobar (no params, no id)"
    )

    # Non-existent method
    check_rpc(
        '{"jsonrpc": "2.0", "method": "does_not_exist", "id": "1"}',
        {"jsonrpc": "2.0", "error": {"code": -32601, "message": "regex:Method.*not found"}, "id": "1"},
        "Non-existent method error"
    )

    # Invalid JSON - use regex to match error since different parsers give different messages
    check_rpc(
        '{"jsonrpc": "2.0", "method": "foobar, "params": "bar", "baz]',
        {"jsonrpc": "2.0", "error": {"code": -32700, "message": "JSON parse error", "data": "regex:Expecting"}, "id": None},
        "Parse error - invalid JSON"
    )

    check_rpc(
        1234,
        {"jsonrpc": "2.0", "error": {"code": -32700, "message": "JSON parse error", "data": "regex:object must be"}, "id": None},
        "Parse error - invalid JSON"
    )

    # Invalid Request object - method is not a string
    check_rpc(
        '{"jsonrpc": "2.0", "method": 1, "params": "bar"}',
        {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request: 'method' must be a string"}, "id": None},
        "Invalid Request - method is number"
    )

    # Missing jsonrpc version
    check_rpc(
        '{"method": "subtract", "params": [1, 2], "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request: 'jsonrpc' must be '2.0'"}, "id": None},
        "Invalid Request - missing jsonrpc field"
    )

    # Wrong jsonrpc version
    check_rpc(
        '{"jsonrpc": "1.0", "method": "subtract", "params": [1, 2], "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request: 'jsonrpc' must be '2.0'"}, "id": None},
        "Invalid Request - wrong jsonrpc version"
    )

    # Missing method
    check_rpc(
        '{"jsonrpc": "2.0", "params": [1, 2], "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request: 'method' is required"}, "id": None},
        "Invalid Request - missing method"
    )

    # Empty array (not valid single request)
    check_rpc(
        '[]',
        {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request: must be a JSON object"}, "id": None},
        "Invalid Request - empty array"
    )

    # Non-object request
    check_rpc(
        '"not an object"',
        {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request: must be a JSON object"}, "id": None},
        "Invalid Request - string instead of object"
    )

    check_rpc(
        '123',
        {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request: must be a JSON object"}, "id": None},
        "Invalid Request - number instead of object"
    )

    # Request with id: null (valid request, not a notification)
    check_rpc(
        '{"jsonrpc": "2.0", "method": "foobar", "id": null}',
        {"jsonrpc": "2.0", "result": "bar", "id": None},
        "Valid request with id: null"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "foobar", "id": 1.5}',
        {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request: 'id' must be a string, integer, or null"}, "id": None},
        "Invalid request with float id"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "foobar", "id": true}',
        {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request: 'id' must be a string, integer, or null"}, "id": None},
        "Invalid request with boolean id"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "async_foobar", "id": "async-id"}',
        {"jsonrpc": "2.0", "result": "async:async-id", "id": "async-id"},
        "Async method dispatch"
    )

    # ========================================
    # ARGUMENT BINDING TESTS
    # ========================================

    # Python handles argument binding; annotations are not revalidated here.
    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": [42], "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32602, "message": "regex:^Invalid params: .*missing.*subtrahend"}, "id": 1},
        "Invalid params - too few positional arguments"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": [42, 23, 10], "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32602, "message": "regex:^Invalid params: .*positional arguments"}, "id": 1},
        "Invalid params - too many positional arguments"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": {"minuend": 42}, "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32602, "message": "regex:^Invalid params: .*missing.*subtrahend"}, "id": 1},
        "Invalid params - missing required parameter"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": {"minuend": 42, "subtrahend": 23, "extra": 1}, "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32602, "message": "regex:^Invalid params: .*unexpected keyword.*extra"}, "id": 1},
        "Invalid params - unexpected parameter"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": [42, "not a number"], "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32603, "message": "regex:unsupported operand"}, "id": 1},
        "Annotations are not runtime validation"
    )

    # Params is invalid type (not array or object)
    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": "invalid", "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32602, "message": "Invalid params: must be array or object"}, "id": 1},
        "Invalid params - string instead of array/object"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": 123, "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32602, "message": "Invalid params: must be array or object"}, "id": 1},
        "Invalid params - number instead of array/object"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": null, "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32602, "message": "regex:^Invalid params: .*missing"}, "id": 1},
        "Invalid params - null for required params"
    )

    # ========================================
    # DEFAULT PARAMETERS TESTS
    # ========================================

    # Function with default param - omit optional param (positional)
    check_rpc(
        '{"jsonrpc": "2.0", "method": "greet", "params": ["Alice"], "id": 1}',
        {"jsonrpc": "2.0", "result": "Hello, Alice!", "id": 1},
        "Default param - omit optional (positional)"
    )

    # Function with default param - provide optional param (positional)
    check_rpc(
        '{"jsonrpc": "2.0", "method": "greet", "params": ["Alice", "Hi"], "id": 1}',
        {"jsonrpc": "2.0", "result": "Hi, Alice!", "id": 1},
        "Default param - provide optional (positional)"
    )

    # Function with default param - omit optional param (named)
    check_rpc(
        '{"jsonrpc": "2.0", "method": "greet", "params": {"name": "Bob"}, "id": 1}',
        {"jsonrpc": "2.0", "result": "Hello, Bob!", "id": 1},
        "Default param - omit optional (named)"
    )

    # Function with default param - provide optional param (named)
    check_rpc(
        '{"jsonrpc": "2.0", "method": "greet", "params": {"name": "Bob", "greeting": "Hey"}, "id": 1}',
        {"jsonrpc": "2.0", "result": "Hey, Bob!", "id": 1},
        "Default param - provide optional (named)"
    )

    # Function with no params - omit params field
    check_rpc(
        '{"jsonrpc": "2.0", "method": "get_data", "id": 1}',
        {"jsonrpc": "2.0", "result": ["hello", 5], "id": 1},
        "No params function - params field omitted"
    )

    # Function with no params - empty array
    check_rpc(
        '{"jsonrpc": "2.0", "method": "get_data", "params": [], "id": 1}',
        {"jsonrpc": "2.0", "result": ["hello", 5], "id": 1},
        "No params function - empty array"
    )

    # Function with no params - empty object
    check_rpc(
        '{"jsonrpc": "2.0", "method": "get_data", "params": {}, "id": 1}',
        {"jsonrpc": "2.0", "result": ["hello", 5], "id": 1},
        "No params function - empty object"
    )

    # ========================================
    # UNION TYPE TESTS
    # ========================================

    # Union type - int
    check_rpc(
        '{"jsonrpc": "2.0", "method": "union_test", "params": [123], "id": 1}',
        {"jsonrpc": "2.0", "result": "ID: 123", "id": 1},
        "Union type (int | str) - int value"
    )

    # Union type - str
    check_rpc(
        '{"jsonrpc": "2.0", "method": "union_test", "params": ["abc"], "id": 1}',
        {"jsonrpc": "2.0", "result": "ID: abc", "id": 1},
        "Union type (int | str) - str value"
    )

    # Type annotations are schema metadata, not runtime validation.
    check_rpc(
        '{"jsonrpc": "2.0", "method": "union_test", "params": [[1, 2, 3]], "id": 1}',
        {"jsonrpc": "2.0", "result": "ID: [1, 2, 3]", "id": 1},
        "Union annotation is not revalidated"
    )

    # Union type - null
    check_rpc(
        '{"jsonrpc": "2.0", "method": "union_test", "params": [null], "id": 1}',
        {"jsonrpc": "2.0", "result": "ID: <nil>", "id": 1},
        "Union type (int | str | None) - null value"
    )

    # Exact union arms take precedence over int-to-float coercion
    check_rpc(
        '{"jsonrpc": "2.0", "method": "exact_union_test", "params": [true], "id": 1}',
        {"jsonrpc": "2.0", "result": "bool:True", "id": 1},
        "Union type - exact bool before float coercion"
    )
    check_rpc(
        '{"jsonrpc": "2.0", "method": "exact_union_test", "params": [1], "id": 1}',
        {"jsonrpc": "2.0", "result": "int:1", "id": 1},
        "Union type - exact int before float coercion"
    )

    # ========================================
    # OPTIONAL TYPE TESTS
    # ========================================

    # Optional - provide value
    check_rpc(
        '{"jsonrpc": "2.0", "method": "process_optional", "params": [42], "id": 1}',
        {"jsonrpc": "2.0", "result": "Got: 42", "id": 1},
        "Optional type - provide value"
    )

    # Optional - provide null
    check_rpc(
        '{"jsonrpc": "2.0", "method": "process_optional", "params": [null], "id": 1}',
        {"jsonrpc": "2.0", "result": "Got: None", "id": 1},
        "Optional type - provide null"
    )

    # ========================================
    # GENERIC TYPE TESTS
    # ========================================

    # list[T] - valid list
    check_rpc(
        '{"jsonrpc": "2.0", "method": "list_test", "params": [["a", "b", "c"]], "id": 1}',
        {"jsonrpc": "2.0", "result": 3, "id": 1},
        "Generic type list[str] - valid list"
    )

    # The function receives decoded JSON directly, regardless of annotations.
    check_rpc(
        '{"jsonrpc": "2.0", "method": "list_test", "params": ["not a list"], "id": 1}',
        {"jsonrpc": "2.0", "result": 10, "id": 1},
        "Generic annotation is not revalidated"
    )

    # Point TypedDict - valid dict
    check_rpc(
        '{"jsonrpc": "2.0", "method": "point_pretty", "params": [{"x": 10, "y": 20}], "id": 1}',
        {"jsonrpc": "2.0", "result": "Point(x=10, y=20)", "id": 1},
        "TypedDict Point - valid dict"
    )

    # Numeric JSON values are passed through without coercion.
    check_rpc(
        '{"jsonrpc": "2.0", "method": "round_float", "params": [3], "id": 1}',
        {"jsonrpc": "2.0", "result": 3, "id": 1},
        "Numeric value passed through"
    )

    # Any type - various inputs
    check_rpc(
        '{"jsonrpc": "2.0", "method": "python_repr", "params": [42], "id": 1}',
        {"jsonrpc": "2.0", "result": "42", "id": 1},
        "Any type - int value"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "python_repr", "params": ["hello"], "id": 1}',
        {"jsonrpc": "2.0", "result": "'hello'", "id": 1},
        "Any type - str value"
    )

    # Unspecified types (unknown) - should accept anything
    check_rpc(
        '{"jsonrpc": "2.0", "method": "unknown", "params": [10, 20], "id": 1}',
        {"jsonrpc": "2.0", "result": 30, "id": 1},
        "Unknown parameter types - accept any"
    )

    # ========================================
    # NOTIFICATION ERROR HANDLING
    # ========================================

    # Notification with error (should return None, no response)
    check_rpc(
        '{"jsonrpc": "2.0", "method": "does_not_exist"}',
        None,
        "Notification - error does not produce response"
    )

    # Notification with invalid params (should return None, no response)
    check_rpc(
        '{"jsonrpc": "2.0", "method": "subtract", "params": [1]}',
        None,
        "Notification - invalid params does not produce response"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "exception", "id": 1}',
        {"jsonrpc": "2.0", "error": {"code": -32603, "message": "regex:Python exception"}, "id": 1},
        "Method that raises python exception"
    )

    check_rpc(
        '{"jsonrpc": "2.0", "method": "exception"}',
        None,
        "Notification - method that raises python exception"
    )

    check_async_method_dispatch()
    check_current_request_id_is_stack_safe()

    print("\n" + "="*60)
    print("ALL TESTS PASSED! ✓")
    print("="*60)

def test_jsonrpc_compliance():
    run_all_tests()

if __name__ == "__main__":
    run_all_tests()
