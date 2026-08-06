import asyncio
import contextvars
import inspect
import json
import traceback
from typing import Any, Callable, TypedDict, TypeAlias, NotRequired



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

    def _call(self, method: str, params: Any) -> Any:
        try:
            func = self.methods[method]
        except KeyError:
            raise JsonRpcException(-32601, f"Method '{method}' not found") from None

        if params is None:
            args, kwargs = (), {}
        elif isinstance(params, list):
            args, kwargs = params, {}
        elif isinstance(params, dict):
            args, kwargs = (), params
        else:
            raise JsonRpcException(-32602, "Invalid params: must be array or object")

        try:
            return func(*args, **kwargs)
        except TypeError:
            # Only inspect the signature on the error path. This distinguishes bad
            # arguments from a TypeError raised inside the function.
            try:
                signature = inspect.signature(func)
            except (TypeError, ValueError):
                raise
            try:
                signature.bind(*args, **kwargs)
            except TypeError as exc:
                raise JsonRpcException(-32602, f"Invalid params: {exc}") from None
            raise

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
