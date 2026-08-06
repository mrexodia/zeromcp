import asyncio
import contextvars
import inspect
import json
import traceback
from collections.abc import Mapping
from typing import Annotated, Any, Callable, Literal, get_type_hints, get_origin, get_args, Union, TypedDict, TypeAlias, NotRequired, Required, is_typeddict
from types import UnionType
from enum import Enum


def _literal_json_value(value: Any) -> Any:
    """Return the recursively normalized JSON wire value of a Literal member."""
    if isinstance(value, Enum):
        return _literal_json_value(value.value)
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, (list, tuple)):
        return [_literal_json_value(item) for item in value]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            wire_key = _literal_json_value(key) if isinstance(key, Enum) else key
            if type(wire_key) is not str:
                raise TypeError(f"Literal mapping key {key!r} is not representable in JSON")
            if wire_key in result:
                raise TypeError(
                    f"Literal mapping key {key!r} normalizes to duplicate JSON key {wire_key!r}"
                )
            result[wire_key] = _literal_json_value(item)
        return result
    raise TypeError(f"Literal member {value!r} is not representable in JSON")


def _json_values_equal(value: Any, wire_value: Any, *, numeric_equivalence: bool) -> bool:
    value_is_number = type(value) in (int, float)
    wire_value_is_number = type(wire_value) in (int, float)
    if value_is_number or wire_value_is_number:
        return (
            value_is_number
            and wire_value_is_number
            and (numeric_equivalence or type(value) is type(wire_value))
            and value == wire_value
        )
    if type(value) is not type(wire_value):
        return False
    if isinstance(value, list):
        return len(value) == len(wire_value) and all(
            _json_values_equal(item, wire_item, numeric_equivalence=numeric_equivalence)
            for item, wire_item in zip(value, wire_value)
        )
    if isinstance(value, dict):
        return value.keys() == wire_value.keys() and all(
            _json_values_equal(value[key], wire_value[key], numeric_equivalence=numeric_equivalence)
            for key in value
        )
    return value == wire_value


def _match_literal(value: Any, members: tuple[Any, ...]) -> tuple[bool, Any]:
    for member in members:
        wire_value = _literal_json_value(member)
        if _json_values_equal(value, wire_value, numeric_equivalence=True):
            return True, member
    return False, value


def _match_json_null(py_type: Any) -> tuple[bool, Any]:
    if py_type is type(None):
        return True, None
    origin = get_origin(py_type)
    args = get_args(py_type)
    if origin is Literal:
        return _match_literal(None, args)
    if origin in (Union, UnionType):
        for arg in args:
            matched, literal_value = _match_json_null(arg)
            if matched:
                return True, literal_value
    return False, None



def _is_async_callable(func: Callable | None) -> bool:
    """Return whether invoking the callable directly creates a coroutine."""
    if func is None:
        return False
    return inspect.iscoroutinefunction(func) or inspect.iscoroutinefunction(getattr(func, "__call__", None))

JsonRpcId: TypeAlias = str | int | None
JsonRpcParams: TypeAlias = dict[str, Any] | list[Any] | None

class JsonRpcRequest(TypedDict):
    jsonrpc: str
    method: str
    params: NotRequired[JsonRpcParams]
    id: NotRequired[JsonRpcId]

class JsonRpcError(TypedDict):
    code: int
    message: str
    data: NotRequired[Any]

class JsonRpcResponse(TypedDict):
    jsonrpc: str
    result: NotRequired[Any]
    error: NotRequired[JsonRpcError]
    id: JsonRpcId

class JsonRpcException(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data

class JsonRpcNoResponse(Exception):
    """Signal that a request was intentionally abandoned without a JSON-RPC response."""

class JsonRpcRegistry:
    def __init__(self):
        self.methods: dict[str, Callable] = {}
        self._cache: dict[Callable, tuple[inspect.Signature, dict, list[str]]] = {}
        self._current_request: contextvars.ContextVar[JsonRpcId] = contextvars.ContextVar("zeromcp_current_request_id", default=None)
        self._async_dispatch: contextvars.ContextVar[bool] = contextvars.ContextVar("zeromcp_async_dispatch", default=False)
        self.redact_exceptions = False

    def current_request_id(self) -> JsonRpcId:
        return self._current_request.get()

    def _in_async_dispatch(self) -> bool:
        return self._async_dispatch.get()

    def method(self, func: Callable, name: str | None = None) -> Callable:
        self.methods[name or getattr(func, "__name__", func.__class__.__name__)] = func
        return func

    def dispatch(self, request: dict | str | bytes | bytearray) -> JsonRpcResponse | None:
        parsed = self._prepare_request(request)
        if isinstance(parsed, dict):
            return parsed
        method, params, request_id, is_notification = parsed

        request_token = self._current_request.set(request_id)
        async_token = self._async_dispatch.set(False)
        try:
            result = self._call(method, params)
            if inspect.isawaitable(result):
                result = self._run_awaitable(method, result)
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "result": result,
                "id": request_id,
            }
        except JsonRpcNoResponse:
            return None
        except JsonRpcException as e:
            if is_notification:
                return None
            return self._error(request_id, e.code, e.message, e.data)
        except Exception as e:
            if is_notification:
                return None
            error = self.map_exception(e)
            return self._error(request_id, error["code"], error["message"], error.get("data"))
        finally:
            self._async_dispatch.reset(async_token)
            self._current_request.reset(request_token)

    async def dispatch_async(self, request: dict | str | bytes | bytearray) -> JsonRpcResponse | None:
        parsed = self._prepare_request(request)
        if isinstance(parsed, dict):
            return parsed
        method, params, request_id, is_notification = parsed

        request_token = self._current_request.set(request_id)
        async_token = self._async_dispatch.set(True)
        try:
            func = self.methods.get(method)
            if _is_async_callable(func):
                result = self._call(method, params)
            else:
                result = await asyncio.to_thread(self._call, method, params)
            if inspect.isawaitable(result):
                result = await result
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "result": result,
                "id": request_id,
            }
        except JsonRpcNoResponse:
            return None
        except JsonRpcException as e:
            if is_notification:
                return None
            return self._error(request_id, e.code, e.message, e.data)
        except Exception as e:
            if is_notification:
                return None
            error = self.map_exception(e)
            return self._error(request_id, error["code"], error["message"], error.get("data"))
        finally:
            self._async_dispatch.reset(async_token)
            self._current_request.reset(request_token)

    def _run_awaitable(self, method: str, awaitable: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(awaitable)

        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        raise JsonRpcException(-32603, f"Method '{method}' is async; use dispatch_async")

    def _prepare_request(self, request: dict | str | bytes | bytearray) -> tuple[str, JsonRpcParams, JsonRpcId, bool] | JsonRpcResponse:
        try:
            if not isinstance(request, dict):
                request = json.loads(request)
            if not isinstance(request, dict):
                return self._error(None, -32600, "Invalid request: must be a JSON object")
        except Exception as e:
            return self._error(None, -32700, "JSON parse error", str(e))

        if request.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid request: 'jsonrpc' must be '2.0'")

        method = request.get("method")
        if method is None:
            return self._error(None, -32600, "Invalid request: 'method' is required")
        if not isinstance(method, str):
            return self._error(None, -32600, "Invalid request: 'method' must be a string")

        request_id: JsonRpcId = request.get("id")
        is_notification = "id" not in request
        if not is_notification and type(request_id) not in (int, str, type(None)):
            return self._error(None, -32600, "Invalid request: 'id' must be a string, integer, or null")

        params: JsonRpcParams = request.get("params")
        return method, params, request_id, is_notification

    def map_exception(self, e: Exception) -> JsonRpcError:
        if self.redact_exceptions:
            return {
                "code": -32603,
                "message": f"Internal Error: {str(e)}",
            }
        return {
            "code": -32603,
            "message": "\n".join(traceback.format_exception(e)).strip() + "\n\nPlease report a bug!",
        }

    def _is_exact_union_match(self, value: Any, expected_type: Any) -> bool:
        if expected_type is Any:
            return False

        origin = get_origin(expected_type)
        args = get_args(expected_type)
        if origin in (Annotated, Required, NotRequired):
            return self._is_exact_union_match(value, args[0])
        if origin is Literal:
            return any(
                _json_values_equal(
                    value,
                    _literal_json_value(member),
                    numeric_equivalence=False,
                )
                for member in args
            )
        if origin in (Union, UnionType):
            return any(self._is_exact_union_match(value, arg_type) for arg_type in args)
        if is_typeddict(expected_type):
            return isinstance(value, dict)
        if origin is not None:
            try:
                return isinstance(value, origin)
            except TypeError:
                return False
        return isinstance(expected_type, type) and type(value) is expected_type

    def _validate_value(self, param_name: str, value: Any, expected_type: Any) -> Any:
        if expected_type is Any:
            return value

        origin = get_origin(expected_type)
        args = get_args(expected_type)

        if origin in (Annotated, Required, NotRequired):
            return self._validate_value(param_name, value, args[0])

        if value is None:
            matched, literal_value = _match_json_null(expected_type)
            if not matched:
                raise JsonRpcException(-32602, f"Invalid params: {param_name} cannot be null")
            return literal_value

        if origin is Literal:
            matched, literal_value = _match_literal(value, args)
            if not matched:
                raise JsonRpcException(
                    -32602,
                    f"Invalid params: {param_name} expected one of {list(args)!r}, got {value!r}",
                )
            return literal_value

        if origin in (Union, UnionType):
            exact_args = [arg_type for arg_type in args if self._is_exact_union_match(value, arg_type)]
            fallback_args = [arg_type for arg_type in args if not self._is_exact_union_match(value, arg_type)]
            for arg_type in (*exact_args, *fallback_args):
                try:
                    return self._validate_value(param_name, value, arg_type)
                except JsonRpcException:
                    pass
            raise JsonRpcException(
                -32602,
                f"Invalid params: {param_name} union does not contain {type(value).__name__}",
            )

        if origin is list:
            if not isinstance(value, list):
                raise JsonRpcException(
                    -32602,
                    f"Invalid params: {param_name} expected list, got {type(value).__name__}",
                )
            item_type = args[0] if args else Any
            return [
                self._validate_value(f"{param_name}[{index}]", item, item_type)
                for index, item in enumerate(value)
            ]

        if origin is dict:
            if not isinstance(value, dict):
                raise JsonRpcException(
                    -32602,
                    f"Invalid params: {param_name} expected dict, got {type(value).__name__}",
                )
            key_type, value_type = args if len(args) == 2 else (Any, Any)
            return {
                self._validate_value(f"{param_name} key", key, key_type): self._validate_value(
                    f"{param_name}[{key!r}]", item, value_type
                )
                for key, item in value.items()
            }

        if is_typeddict(expected_type):
            if not isinstance(value, dict):
                raise JsonRpcException(
                    -32602,
                    f"Invalid params: {param_name} expected dict, got {type(value).__name__}",
                )
            field_types = get_type_hints(expected_type, include_extras=True)
            return {
                key: self._validate_value(f"{param_name}.{key}", item, field_types[key])
                if key in field_types else item
                for key, item in value.items()
            }

        if origin is not None:
            if not isinstance(value, origin):
                raise JsonRpcException(
                    -32602,
                    f"Invalid params: {param_name} expected {origin.__name__}, got {type(value).__name__}",
                )
            return value

        if isinstance(expected_type, type):
            if expected_type is float and isinstance(value, int):
                return float(value)
            if not isinstance(value, expected_type):
                raise JsonRpcException(
                    -32602,
                    f"Invalid params: {param_name} expected {expected_type.__name__}, got {type(value).__name__}",
                )

        return value

    def _call(self, method: str, params: Any) -> Any:
        if method not in self.methods:
            raise JsonRpcException(-32601, f"Method '{method}' not found")

        func = self.methods[method]

        # Check for cached reflection data
        if func not in self._cache:
            sig = inspect.signature(func)
            hints = get_type_hints(func)
            hints.pop("return", None)

            # Determine required vs optional parameters
            required_params = []
            for param_name, param in sig.parameters.items():
                if param.default is inspect.Parameter.empty:
                    required_params.append(param_name)

            self._cache[func] = (sig, hints, required_params)

        sig, hints, required_params = self._cache[func]

        # Handle None params
        if params is None:
            if len(required_params) == 0:
                return func()
            else:
                raise JsonRpcException(-32602, "Missing required params")

        # Convert list params to dict by parameter names
        if isinstance(params, list):
            if len(params) < len(required_params):
                raise JsonRpcException(
                    -32602,
                    f"Invalid params: expected at least {len(required_params)} arguments, got {len(params)}"
                )
            if len(params) > len(sig.parameters):
                raise JsonRpcException(
                    -32602,
                    f"Invalid params: expected at most {len(sig.parameters)} arguments, got {len(params)}"
                )
            params = dict(zip(sig.parameters.keys(), params))

        # Validate dict params
        if isinstance(params, dict):
            # Check all required params are present
            missing = set(required_params) - set(params.keys())
            if missing:
                raise JsonRpcException(
                    -32602,
                    f"Invalid params: missing required parameters: {list(missing)}"
                )

            # Check no extra params
            extra = set(params.keys()) - set(sig.parameters.keys())
            if extra:
                raise JsonRpcException(
                    -32602,
                    f"Invalid params: unexpected parameters: {list(extra)}"
                )

            validated_params = {}
            for param_name, value in params.items():
                # If no type hint, pass through without validation
                if param_name not in hints:
                    validated_params[param_name] = value
                    continue

                validated_params[param_name] = self._validate_value(param_name, value, hints[param_name])

            return func(**validated_params)

        else:
            raise JsonRpcException(-32602, "Invalid params: must be array or object")

    def _error(self, request_id: JsonRpcId, code: int, message: str, data: Any = None) -> JsonRpcResponse:
        error: JsonRpcError = {
            "code": code,
            "message": message,
        }
        if data is not None:
            error["data"] = data
        return {
            "jsonrpc": "2.0",
            "error": error,
            "id": request_id,
        }
