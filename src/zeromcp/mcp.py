import re
import sys
import time
import uuid
import json
import zlib
import ipaddress
import inspect
import threading
import traceback
import asyncio
import contextvars
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer, HTTPServer
from typing import Any, Callable, Union, Annotated, BinaryIO, Mapping, NotRequired, Required, get_origin, get_args, get_type_hints, is_typeddict
from types import UnionType
from urllib.parse import urlparse, parse_qs
from io import BufferedIOBase

from .jsonrpc import JsonRpcRegistry, JsonRpcError, JsonRpcException, JsonRpcNoResponse, _is_async_callable

# Deliberately not the newest supported version: older, half-compliant clients
# are more likely to work when negotiation falls back to 2025-06-18.
MCP_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_STREAMABLE_HTTP_PROTOCOL_VERSION = "2025-03-26"
STREAMABLE_HTTP_PROTOCOL_VERSIONS = {DEFAULT_STREAMABLE_HTTP_PROTOCOL_VERSION, MCP_PROTOCOL_VERSION, "2025-11-25"}
LEGACY_SSE_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOL_VERSIONS = STREAMABLE_HTTP_PROTOCOL_VERSIONS | {LEGACY_SSE_PROTOCOL_VERSION}

@dataclass(frozen=True)
class McpAuthInfo:
    subject: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    claims: Mapping[str, Any] = field(default_factory=dict)

class _McpCancellation:
    """Thread-safe bridge from an MCP cancellation notification to a task."""

    def __init__(self, loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> None:
        self._loop = loop
        self._task = task
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self, reason: str | None = None) -> None:
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        self._loop.call_soon_threadsafe(self._task.cancel, reason)


@dataclass(frozen=True)
class McpRequestContext:
    request_id: int | str | None = None
    meta: dict[str, Any] | None = None
    auth: McpAuthInfo | None = None
    protocol_version: str | None = None
    transport_session_id: str | None = None

@dataclass(frozen=True)
class McpOAuthConfig:
    resource: str
    authorization_servers: tuple[str, ...]
    verify_token: Callable[[str, str], McpAuthInfo | None]
    scopes_supported: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()
    resource_metadata_url: str | None = None

class McpToolError(Exception):
    def __init__(self, message: str):
        super().__init__(message)

class McpRpcRegistry(JsonRpcRegistry):
    """JSON-RPC registry with custom error handling for MCP tools"""
    def map_exception(self, e: Exception) -> JsonRpcError:
        if isinstance(e, McpToolError):
            return {
                "code": -32000,
                "message": e.args[0] or "MCP Tool Error",
            }
        return super().map_exception(e)

class _McpSseConnection:
    """Manages a single SSE client connection"""
    def __init__(self, wfile):
        self.wfile: BufferedIOBase = wfile
        self.session_id = str(uuid.uuid4())
        self.alive = True

    def send_event(self, event_type: str, data):
        """Send an SSE event to the client

        Args:
            event_type: Type of event (e.g., "endpoint", "message", "ping")
            data: Event data - can be string (sent as-is) or dict (JSON-encoded)
        """
        if not self.alive:
            return False

        try:
            # SSE format: "event: type\ndata: content\n\n"
            if isinstance(data, str):
                data_str = f"data: {data}\n\n"
            else:
                data_str = f"data: {json.dumps(data)}\n\n"
            message = f"event: {event_type}\n{data_str}".encode("utf-8")
            self.wfile.write(message)
            self.wfile.flush()  # Ensure data is sent immediately
            return True
        except (BrokenPipeError, OSError):
            self.alive = False
            return False

def _origin_allowed_by_policy(
    allowed: Callable[[str], bool] | list[str] | str | None,
    origin: str,
) -> bool:
    if not origin or allowed is None:
        return False
    if isinstance(allowed, str):
        return allowed == "*" or origin == allowed
    if isinstance(allowed, list):
        return "*" in allowed or origin in allowed
    return allowed(origin)

def _parse_host_header(host_header: str | None) -> str | None:
    if not host_header:
        return None

    host_header = host_header.strip()
    if not host_header:
        return None

    if host_header.startswith("["):
        end = host_header.find("]")
        if end == -1:
            return None
        return host_header[1:end]

    if host_header.count(":") == 1:
        return host_header.rsplit(":", 1)[0]

    return host_header

def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"

def _host_header_allowed_for_bind(bound_host: str, host_header: str | None) -> bool:
    if host_header is None:
        return True

    host_name = _parse_host_header(host_header)
    if host_name is None:
        return False

    if not _is_loopback_host(bound_host):
        return True

    return _is_loopback_host(host_name)

class McpHttpRequestHandler(BaseHTTPRequestHandler):
    server_version = "zeromcp/1.3.0"
    error_message_format = "%(code)d - %(message)s"
    error_content_type = "text/plain"

    def __init__(self, request, client_address, server):
        self.mcp_server: "McpServer" = getattr(server, "mcp_server")
        super().__init__(request, client_address, server)

    def log_message(self, format, *args):
        """Override to suppress default logging or customize"""
        pass

    def send_cors_headers(self, *, preflight = False):
        origin = self.headers.get("Origin", "")
        if not _origin_allowed_by_policy(self.mcp_server.cors_allowed_origins, origin):
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Expose-Headers", "Mcp-Session-Id, MCP-Protocol-Version, WWW-Authenticate")
        if preflight:
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept, Authorization, X-Requested-With, Mcp-Session-Id, MCP-Protocol-Version")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")

    def send_error(self, code, message=None, explain=None):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(f"{message}\n".encode("utf-8"))

    def _quote_auth_param(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def _oauth_resource_metadata_url(self) -> str:
        config = self.mcp_server._oauth_config
        if config is not None and config.resource_metadata_url is not None:
            return config.resource_metadata_url
        host = self.headers.get("Host")
        if not host:
            server_address = getattr(self.server, "server_address", ("127.0.0.1", 0))
            host = f"{server_address[0]}:{server_address[1]}" if isinstance(server_address, tuple) else "127.0.0.1"
        scheme = self.headers.get("X-Forwarded-Proto", "http").split(",", 1)[0].strip() or "http"
        return f"{scheme}://{host}/.well-known/oauth-protected-resource"

    def _send_oauth_error(self, status: int, message: str, *, error: str | None = None, scope: str | None = None) -> None:
        challenge = f'Bearer resource_metadata="{self._quote_auth_param(self._oauth_resource_metadata_url())}"'
        if error is not None:
            challenge += f', error="{self._quote_auth_param(error)}"'
        if scope is not None:
            challenge += f', scope="{self._quote_auth_param(scope)}"'

        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("WWW-Authenticate", challenge)
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(f"{message}\n".encode("utf-8"))

    def _check_oauth_for_path(self, path: str) -> tuple[bool, McpAuthInfo | None]:
        if path not in ("/sse", "/mcp"):
            return True, None
        return self._check_oauth()

    def _check_oauth(self) -> tuple[bool, McpAuthInfo | None]:
        config = self.mcp_server._oauth_config
        if config is None:
            return True, None

        auth = self.headers.get("Authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            self._send_oauth_error(401, "Authorization required", scope=" ".join(config.required_scopes) or None)
            return False, None

        try:
            auth_info = config.verify_token(token.strip(), config.resource)
        except Exception:
            traceback.print_exc()
            auth_info = None
        if auth_info is None:
            self._send_oauth_error(401, "Invalid access token", error="invalid_token", scope=" ".join(config.required_scopes) or None)
            return False, None

        missing_scopes = set(config.required_scopes) - set(auth_info.scopes)
        if missing_scopes:
            self._send_oauth_error(403, "Insufficient scope", error="insufficient_scope", scope=" ".join(config.required_scopes))
            return False, None

        return True, auth_info

    def _is_oauth_metadata_path(self, path: str) -> bool:
        return path == "/.well-known/oauth-protected-resource" or path.startswith("/.well-known/oauth-protected-resource/")

    def _handle_oauth_protected_resource_metadata(self) -> None:
        config = self.mcp_server._oauth_config
        if config is None:
            self.send_error(404, "Not Found")
            return

        metadata: dict[str, Any] = {
            "resource": config.resource,
            "authorization_servers": list(config.authorization_servers),
            "bearer_methods_supported": ["header"],
        }
        if config.scopes_supported:
            metadata["scopes_supported"] = list(config.scopes_supported)

        body = json.dumps(metadata).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def handle(self):
        """Override to add error handling for connection errors"""
        try:
            super().handle()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # Client disconnected - normal, suppress traceback
            pass

    def _check_api_request(self) -> bool:
        server_address = getattr(self.server, "server_address", ("", 0))
        bound_host = str(server_address[0]) if isinstance(server_address, tuple) else ""
        if not _host_header_allowed_for_bind(bound_host, self.headers.get("Host")):
            self.send_error(403, "Invalid Host")
            return False

        origin = self.headers.get("Origin", "")
        if origin and not _origin_allowed_by_policy(self.mcp_server.cors_allowed_origins, origin):
            self.send_error(403, "Invalid Origin")
            return False
        return True

    def do_GET(self):
        if not self._check_api_request():
            return
        path = urlparse(self.path).path
        if self._is_oauth_metadata_path(path):
            self._handle_oauth_protected_resource_metadata()
            return

        ok, auth_info = self._check_oauth_for_path(path)
        if not ok:
            return

        match path:
            case "/sse":
                self._handle_sse_get(auth_info)
            case "/mcp":
                self.send_error(405, "Method Not Allowed")
            case _:
                self.send_error(404, "Not Found")

    def do_POST(self):
        if not self._check_api_request():
            return

        path = urlparse(self.path).path
        ok, auth_info = self._check_oauth_for_path(path)
        if not ok:
            return

        body = self._read_body()
        if body is None:
            return

        match path:
            case "/sse":
                self._handle_sse_post(body, auth_info)
            case "/mcp":
                self._handle_mcp_post(body, auth_info)
            case _:
                self.send_error(404, "Not Found")

    def do_OPTIONS(self):
        """Handle CORS preflight requests"""
        if not self._check_api_request():
            return
        self.send_response(200)
        self.send_cors_headers(preflight=True)
        self.end_headers()

    def do_DELETE(self):
        if not self._check_api_request():
            return
        path = urlparse(self.path).path
        ok, _ = self._check_oauth_for_path(path)
        if not ok:
            return
        if path != "/mcp":
            self.send_error(405, "Method Not Allowed")
            return

        # Explicit session termination (MCP Streamable HTTP session management).
        session_id = self.headers.get("Mcp-Session-Id")
        if not session_id:
            self.send_error(400, "Missing Mcp-Session-Id")
            return
        if not self.mcp_server.unregister_http_session(session_id):
            self.send_error(404, "Session not found")
            return
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def _read_body(self) -> bytes | None:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            raw = self._read_chunked()
            if raw is None:
                return None
        else:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > self.mcp_server.post_body_limit:
                self._send_payload_too_large()
                return None
            raw = self.rfile.read(content_length) if content_length > 0 else b""

        if len(raw) > self.mcp_server.post_body_limit:
            self._send_payload_too_large()
            return None

        return self._decompress_body(raw)

    def _send_payload_too_large(self) -> None:
        self.close_connection = True
        self.send_error(413, f"Payload Too Large: exceeds {self.mcp_server.post_body_limit} bytes")

    def _read_chunked(self) -> bytes | None:
        body = b""
        limit = self.mcp_server.post_body_limit
        while True:
            line = self.rfile.readline().split(b";")[0].strip()
            chunk_size = int(line, 16)
            if chunk_size == 0:
                # Consume trailer fields until blank line
                while self.rfile.readline().strip():
                    pass
                break

            if len(body) + chunk_size > limit:
                self._send_payload_too_large()
                return None

            body += self.rfile.read(chunk_size)
            self.rfile.readline()
        return body

    def _decompress_limited(self, decompressor: Any, data: bytes, wbits: int = 0) -> bytes | None:
        limit = self.mcp_server.post_body_limit
        output = decompressor.decompress(data, limit + 1)
        if len(output) > limit:
            self._send_payload_too_large()
            return None

        while wbits and decompressor.unused_data:
            remaining = decompressor.unused_data
            decompressor = zlib.decompressobj(wbits)
            output += decompressor.decompress(remaining, limit + 1 - len(output))
            if len(output) > limit:
                self._send_payload_too_large()
                return None
        return output

    def _decompress_body(self, data: bytes) -> bytes | None:
        encoding = self.headers.get("Content-Encoding", "").lower().strip()
        try:
            if encoding in ("gzip", "x-gzip"):
                wbits = 16 + zlib.MAX_WBITS
                return self._decompress_limited(zlib.decompressobj(wbits), data, wbits=wbits)
            elif encoding == "deflate":
                wbits = zlib.MAX_WBITS if data[:1] == b'\x78' else -zlib.MAX_WBITS
                return self._decompress_limited(zlib.decompressobj(wbits), data)
        except zlib.error:
            self.send_error(400, "Invalid compressed request body")
            return None
        return data

    def _handle_sse_get(self, auth_info: McpAuthInfo | None = None):
        # Create SSE connection wrapper
        conn = _McpSseConnection(self.wfile)
        self.mcp_server._sse_connections[conn.session_id] = conn

        try:
            # Send SSE headers
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_cors_headers()
            self.end_headers()

            # Send endpoint event with session ID for routing
            conn.send_event("endpoint", f"/sse?session={conn.session_id}")

            # Keep connection alive with periodic pings
            last_ping = time.time()
            while conn.alive and self.mcp_server._running:
                now = time.time()
                if now - last_ping > 30:  # Ping every 30 seconds
                    if not conn.send_event("ping", {}):
                        break
                    last_ping = now
                time.sleep(1)

        finally:
            conn.alive = False
            if conn.session_id in self.mcp_server._sse_connections:
                del self.mcp_server._sse_connections[conn.session_id]

    def _handle_sse_post(self, body: bytes, auth_info: McpAuthInfo | None = None):
        query_params = parse_qs(urlparse(self.path).query)
        session_id = query_params.get("session", [None])[0]
        if session_id is None:
            self.send_error(400, "Missing ?session for SSE POST")
            return

        # Validate the SSE session before dispatching; otherwise a tool can run
        # with nowhere to send its response.
        sse_conn = self.mcp_server._sse_connections.get(session_id)
        if sse_conn is None or not sse_conn.alive:
            self.send_error(400, f"No active SSE connection found for session {session_id}")
            return

        # Dispatch to MCP registry.
        request_id = self.mcp_server._request_id_from_body(body)
        with self.mcp_server._context_scope(
            request_id=request_id,
            auth=auth_info,
            protocol_version=LEGACY_SSE_PROTOCOL_VERSION,
            transport_session_id=f"sse:{session_id}",
        ):
            response = self.mcp_server._dispatch_mcp(body)

        # Send SSE response if necessary.
        if response is not None:
            # Send response via SSE event stream.
            sse_conn.send_event("message", response)

        # Return 202 Accepted to acknowledge POST
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _handle_mcp_post(self, body: bytes, auth_info: McpAuthInfo | None = None):
        # The MCP-Protocol-Version header only exists for Streamable HTTP protocol
        # versions; 2024-11-05 clients are served by the legacy /sse transport.
        protocol_version = self.headers.get("MCP-Protocol-Version")
        if protocol_version is not None and protocol_version not in STREAMABLE_HTTP_PROTOCOL_VERSIONS:
            self.send_error(400, "Unsupported MCP-Protocol-Version")
            return

        parsed = None
        request_method: str | None = None
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                method = parsed.get("method")
                if isinstance(method, str):
                    request_method = method
        except Exception:
            pass

        requested_protocol_version: str | None = None
        if request_method == "initialize" and isinstance(parsed, dict):
            params = parsed.get("params")
            if isinstance(params, dict):
                protocol_version_param = params.get("protocolVersion")
                if isinstance(protocol_version_param, str):
                    requested_protocol_version = protocol_version_param

        incoming_session_id = self.headers.get("Mcp-Session-Id")
        request_session_id = incoming_session_id
        response_session_id = incoming_session_id
        if request_method == "initialize":
            request_session_id = str(uuid.uuid4())
            response_session_id = None
        elif self.mcp_server.require_streamable_http_session:
            if request_session_id is None:
                self.send_error(400, "Missing Mcp-Session-Id")
                return
            if not self.mcp_server.has_http_session(request_session_id):
                self.send_error(404, "Session not found")
                return

        def send_response(status: int, response_body: bytes):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            if response_session_id is not None:
                self.send_header("Mcp-Session-Id", response_session_id)
            self.send_cors_headers()
            self.end_headers()
            if response_body:
                self.wfile.write(response_body)

        # JSON-RPC responses sent by clients are accepted; this server does not issue client requests.
        if isinstance(parsed, dict) and request_method is None and "id" in parsed and ("result" in parsed or "error" in parsed):
            send_response(202, b"")
            return

        if request_method == "initialize":
            active_protocol_version = (
                requested_protocol_version
                if requested_protocol_version is not None and requested_protocol_version in SUPPORTED_PROTOCOL_VERSIONS
                else MCP_PROTOCOL_VERSION
            )
        else:
            active_protocol_version = protocol_version
            if active_protocol_version is None and incoming_session_id is not None:
                active_protocol_version = self.mcp_server.get_http_session_protocol(incoming_session_id)
            if active_protocol_version is None:
                active_protocol_version = DEFAULT_STREAMABLE_HTTP_PROTOCOL_VERSION

        # Dispatch to MCP registry.
        request_id = self.mcp_server._request_id_from_body(parsed) if isinstance(parsed, dict) else None
        transport_session_id = f"http:{request_session_id}" if request_session_id else f"http:anonymous:{uuid.uuid4()}"
        with self.mcp_server._context_scope(
            request_id=request_id,
            auth=auth_info,
            protocol_version=active_protocol_version,
            transport_session_id=transport_session_id,
        ):
            response = self.mcp_server._dispatch_mcp(body)

        if request_method == "initialize" and response is not None and "error" not in response:
            assert request_session_id is not None
            response_session_id = request_session_id
            if self.mcp_server.require_streamable_http_session:
                self.mcp_server.register_http_session(request_session_id, active_protocol_version)
            else:
                self.mcp_server.remember_http_session_protocol(request_session_id, active_protocol_version)

        # Check if notification (returns None)
        if response is None:
            send_response(202, b"")
        else:
            send_response(200, json.dumps(response).encode("utf-8"))

class McpServer:
    def __init__(self, name: str, version: str = "1.0.0", instructions: str | None = None):
        self.name = name
        self.version = version
        self.instructions = instructions
        self.post_body_limit = 10 * 1024 * 1024
        self.cors_allowed_origins: Callable[[str], bool] | list[str] | str | None = self.cors_localhost
        self.tools = McpRpcRegistry()
        self.resources = McpRpcRegistry()
        self.prompts = McpRpcRegistry()

        self._http_server: HTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._running = False
        self._sse_connections: dict[str, _McpSseConnection] = {}
        # Session state is LRU-bounded by max_http_sessions; evicted or DELETEd
        # sessions get 404 and the client starts a new session per the MCP spec.
        self._http_sessions: OrderedDict[str, None] = OrderedDict()
        self._http_session_protocol_versions: OrderedDict[str, str] = OrderedDict()
        self._http_sessions_lock = threading.Lock()
        self.max_http_sessions = 1024
        self._stdio_protocol_version: str | None = None
        self._pending_requests: dict[tuple[str | None, int | str], _McpCancellation] = {}
        self._pending_requests_lock = threading.Lock()
        self._context_var: contextvars.ContextVar[McpRequestContext] = contextvars.ContextVar(
            f"zeromcp_request_context_{id(self)}",
            default=McpRequestContext(),
        )
        self._oauth_config: McpOAuthConfig | None = None
        self.require_streamable_http_session = False

        # Register MCP protocol methods with correct names.
        self.registry = JsonRpcRegistry()
        self.registry.method(self._mcp_ping, "ping")
        self.registry.method(self._mcp_initialize, "initialize")
        self.registry.method(self._mcp_tools_list, "tools/list")
        self.registry.method(self._mcp_tools_call, "tools/call")
        self.registry.method(self._mcp_resources_list, "resources/list")
        self.registry.method(self._mcp_resource_templates_list, "resources/templates/list")
        self.registry.method(self._mcp_resources_read, "resources/read")
        self.registry.method(self._mcp_prompts_list, "prompts/list")
        self.registry.method(self._mcp_prompts_get, "prompts/get")
        self.registry.method(self._mcp_notifications_initialized, "notifications/initialized")
        self.registry.method(self._mcp_notifications_cancelled, "notifications/cancelled")

    @property
    def context(self) -> McpRequestContext:
        return self._context_var.get()

    @contextmanager
    def _context_scope(self, **updates):
        token = self._context_var.set(replace(self.context, **updates))
        try:
            yield
        finally:
            self._context_var.reset(token)

    def oauth(
        self,
        *,
        resource: str,
        authorization_servers: list[str] | tuple[str, ...],
        scopes_supported: list[str] | tuple[str, ...] | None = None,
        required_scopes: list[str] | tuple[str, ...] | None = None,
        resource_metadata_url: str | None = None,
    ) -> Callable[[Callable[[str, str], McpAuthInfo | None]], Callable[[str, str], McpAuthInfo | None]]:
        if not authorization_servers:
            raise ValueError("authorization_servers must not be empty")

        def decorator(verify_token: Callable[[str, str], McpAuthInfo | None]) -> Callable[[str, str], McpAuthInfo | None]:
            self._oauth_config = McpOAuthConfig(
                resource=resource,
                authorization_servers=tuple(authorization_servers),
                verify_token=verify_token,
                scopes_supported=tuple(scopes_supported or ()),
                required_scopes=tuple(required_scopes or ()),
                resource_metadata_url=resource_metadata_url,
            )
            return verify_token

        return decorator

    def tool(
        self,
        func: Callable | None = None,
        *,
        title: str | None = None,
        read_only: bool | None = None,
        destructive: bool | None = None,
        idempotent: bool | None = None,
        open_world: bool | None = None,
    ) -> Callable:
        annotations = {}
        if read_only is not None:
            annotations["readOnlyHint"] = read_only
        if destructive is not None:
            annotations["destructiveHint"] = destructive
        if idempotent is not None:
            annotations["idempotentHint"] = idempotent
        if open_world is not None:
            annotations["openWorldHint"] = open_world

        def decorator(inner: Callable) -> Callable:
            if title is not None:
                setattr(inner, "__mcp_tool_title__", title)
            if annotations:
                setattr(inner, "__mcp_tool_annotations__", annotations)
            return self.tools.method(inner)

        return decorator if func is None else decorator(func)

    def prompt(self, func: Callable) -> Callable:
        return self.prompts.method(func)

    def resource(self, uri: str) -> Callable[[Callable], Callable]:
        def decorator(func: Callable) -> Callable:
            setattr(func, "__resource_uri__", uri)
            return self.resources.method(func)
        return decorator

    def serve(self, host: str, port: int, *, background = True, request_handler = McpHttpRequestHandler):
        if self._running:
            print("[MCP] Server is already running")
            return

        # Create server with deferred binding
        assert issubclass(request_handler, McpHttpRequestHandler)
        self._http_server = (ThreadingHTTPServer if background else HTTPServer)(
            (host, port), request_handler, bind_and_activate=False
        )
        self._http_server.allow_reuse_address = False

        # Set the MCPServer instance on the handler class
        setattr(self._http_server, "mcp_server", self)

        try:
            # Bind and activate in main thread - errors propagate synchronously
            self._http_server.server_bind()
            self._http_server.server_activate()
        except OSError:
            # Cleanup on binding failure
            self._http_server.server_close()
            self._http_server = None
            raise

        # Only start thread after successful bind
        self._running = True

        print("[MCP] Server started:")
        print(f"  Streamable HTTP: http://{host}:{port}/mcp")
        print(f"  SSE: http://{host}:{port}/sse")

        def serve_forever():
            try:
                self._http_server.serve_forever()  # type: ignore
            except Exception as e:
                print(f"[MCP] Server error: {e}")
                traceback.print_exc()
            finally:
                self._running = False

        if background:
            self._server_thread = threading.Thread(target=serve_forever, daemon=True)
            self._server_thread.start()
        else:
            serve_forever()

    def stop(self):
        if not self._running:
            return

        self._running = False

        # Close all SSE connections
        for conn in self._sse_connections.values():
            conn.alive = False
        self._sse_connections.clear()

        # Shutdown the HTTP server
        if self._http_server:
            # shutdown() must be called from a different thread
            # than the one running serve_forever()
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None

        if self._server_thread:
            self._server_thread.join()
            self._server_thread = None

        print("[MCP] Server stopped")

    def stdio(self, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None):
        stdin = stdin or sys.stdin.buffer
        stdout = stdout or sys.stdout.buffer
        while True:
            try:
                request = stdin.readline()
                if not request:  # EOF
                    break

                # Strip whitespace (trailing newline) before parsing
                request = request.strip()
                if not request:
                    continue

                request_id = self._request_id_from_body(request)
                request_method, active_protocol_version = self._stdio_request_protocol(request)
                with self._context_scope(
                    request_id=request_id,
                    protocol_version=active_protocol_version,
                    transport_session_id="stdio:default",
                ):
                    response = self._dispatch_mcp(request)
                if request_method == "initialize" and response is not None and "error" not in response:
                    self._stdio_protocol_version = active_protocol_version
                if response is not None:
                    stdout.write(json.dumps(response).encode("utf-8") + b"\n")
                    stdout.flush()
            except (BrokenPipeError, KeyboardInterrupt):  # Client disconnected
                break

    async def stdio_async(self, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None):
        stdin = stdin or sys.stdin.buffer
        stdout = stdout or sys.stdout.buffer
        write_lock = asyncio.Lock()
        tasks: set[asyncio.Task] = set()

        async def write_response(response):
            if response is None:
                return
            async with write_lock:
                stdout.write(json.dumps(response).encode("utf-8") + b"\n")
                stdout.flush()

        async def handle_request(request: bytes):
            request_id = self._request_id_from_body(request)
            request_method, active_protocol_version = self._stdio_request_protocol(request)
            with self._context_scope(
                request_id=request_id,
                protocol_version=active_protocol_version,
                transport_session_id="stdio:default",
            ):
                response = await self._dispatch_mcp_async(request)
            # Concurrent tasks race on this attribute, but initialize is the first
            # request in practice and stale reads only see the default version.
            if request_method == "initialize" and response is not None and "error" not in response:
                self._stdio_protocol_version = active_protocol_version
            await write_response(response)

        while True:
            try:
                request = await asyncio.to_thread(stdin.readline)
                if not request:  # EOF
                    break

                # Strip whitespace (trailing newline) before parsing
                request = request.strip()
                if not request:
                    continue

                task = asyncio.create_task(handle_request(request))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            except (BrokenPipeError, KeyboardInterrupt):  # Client disconnected
                break

        if tasks:
            await asyncio.gather(*tasks)

    def _stdio_request_protocol(self, body: dict | bytes | bytearray) -> tuple[str | None, str]:
        try:
            request = body if isinstance(body, dict) else json.loads(body)
        except Exception:
            return None, self._stdio_protocol_version or MCP_PROTOCOL_VERSION
        if not isinstance(request, dict):
            return None, self._stdio_protocol_version or MCP_PROTOCOL_VERSION

        method = request.get("method")
        if method != "initialize":
            return method if isinstance(method, str) else None, self._stdio_protocol_version or MCP_PROTOCOL_VERSION

        requested_protocol_version: str | None = None
        params = request.get("params")
        if isinstance(params, dict):
            protocol_version_param = params.get("protocolVersion")
            if isinstance(protocol_version_param, str):
                requested_protocol_version = protocol_version_param
        active_protocol_version = (
            requested_protocol_version
            if requested_protocol_version is not None and requested_protocol_version in SUPPORTED_PROTOCOL_VERSIONS
            else MCP_PROTOCOL_VERSION
        )
        return method, active_protocol_version

    def get_current_transport_session_id(self) -> str | None:
        return self.context.transport_session_id

    def remember_http_session_protocol(self, session_id: str, protocol_version: str) -> None:
        with self._http_sessions_lock:
            self._http_session_protocol_versions[session_id] = protocol_version
            self._http_session_protocol_versions.move_to_end(session_id)
            self._evict_http_sessions()

    def get_http_session_protocol(self, session_id: str) -> str | None:
        with self._http_sessions_lock:
            protocol_version = self._http_session_protocol_versions.get(session_id)
            if protocol_version is not None:
                self._http_session_protocol_versions.move_to_end(session_id)
            return protocol_version

    def register_http_session(self, session_id: str, protocol_version: str | None = None) -> None:
        with self._http_sessions_lock:
            self._http_sessions[session_id] = None
            self._http_sessions.move_to_end(session_id)
            if protocol_version is not None:
                self._http_session_protocol_versions[session_id] = protocol_version
                self._http_session_protocol_versions.move_to_end(session_id)
            self._evict_http_sessions()

    def has_http_session(self, session_id: str) -> bool:
        with self._http_sessions_lock:
            if session_id not in self._http_sessions:
                return False
            self._http_sessions.move_to_end(session_id)
            return True

    def unregister_http_session(self, session_id: str) -> bool:
        """Terminate a session; returns whether the session was known."""
        with self._http_sessions_lock:
            known = session_id in self._http_sessions or session_id in self._http_session_protocol_versions
            self._http_sessions.pop(session_id, None)
            self._http_session_protocol_versions.pop(session_id, None)
            return known

    def _evict_http_sessions(self) -> None:
        # Caller must hold _http_sessions_lock.
        while len(self._http_sessions) > self.max_http_sessions:
            evicted, _ = self._http_sessions.popitem(last=False)
            self._http_session_protocol_versions.pop(evicted, None)
        while len(self._http_session_protocol_versions) > self.max_http_sessions:
            self._http_session_protocol_versions.popitem(last=False)

    def cors_localhost(self, origin: str) -> bool:
        """Allow CORS requests from localhost on ANY port."""
        return urlparse(origin).hostname in ("localhost", "127.0.0.1", "::1")

    def _request_id_from_body(self, body: dict | bytes | bytearray) -> int | str | None:
        try:
            request = body if isinstance(body, dict) else json.loads(body)
        except Exception:
            return None
        if not isinstance(request, dict) or "id" not in request:
            return None
        request_id = request.get("id")
        return request_id if type(request_id) in (int, str) else None

    def _dispatch_mcp(self, request: dict | str | bytes | bytearray):
        return self.registry.dispatch(request)

    async def _dispatch_mcp_async(self, request: dict | str | bytes | bytearray):
        return await self.registry.dispatch_async(request)

    def _match_resource(self, uri: str) -> tuple[str, list[str]] | None:
        # Try to match URI against all registered resource patterns.
        for pattern, name, _ in self._enumerate_resources():
            # Convert pattern to regex, replacing {param} with named capture groups.
            regex_pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
            regex_pattern = f"^{regex_pattern}$"
            match = re.match(regex_pattern, uri)
            if match:
                return name, list(match.groupdict().values())
        return None

    def _mcp_ping(self, _meta: dict | None = None) -> dict:
        """MCP ping method"""
        return {}

    def _mcp_initialize(self, protocolVersion: str, capabilities: dict, clientInfo: dict, _meta: dict | None = None) -> dict:
        """MCP initialize method"""
        result = {
            "protocolVersion": self.context.protocol_version or protocolVersion,
            "capabilities": {
                "tools": {},
                "resources": {
                    "subscribe": False,
                    "listChanged": False,
                },
                "prompts": {},
            },
            "serverInfo": {
                "name": self.name,
                "version": self.version,
            },
        }
        if self.instructions is not None:
            result["instructions"] = self.instructions
        return result

    def _mcp_tools_list(self, cursor: str | None = None, _meta: dict | None = None) -> dict:
        """MCP tools/list method"""
        return {
            "tools": [
                self._generate_tool_schema(func_name, func)
                for func_name, func in self.tools.methods.items()
            ],
        }

    def _mcp_tools_call(self, name: str, arguments: dict | None = None, _meta: dict | None = None):
        """MCP tools/call method"""
        # Wrap tool call in JSON-RPC request so argument validation stays shared.
        return self._dispatch_nested_mcp(
            self.tools,
            {
                "jsonrpc": "2.0",
                "method": name,
                "params": arguments,
                "id": 0,
            },
            lambda tool_response: self._format_tool_response(name, tool_response),
            meta=_meta,
            cancellable=_is_async_callable(self.tools.methods.get(name)),
        )

    def _dispatch_nested_mcp(
        self,
        registry: JsonRpcRegistry,
        request: dict[str, Any],
        formatter: Callable[[Mapping[str, Any]], dict],
        *,
        meta: dict | None,
        cancellable: bool = False,
    ):
        if self.registry._in_async_dispatch():
            return self._dispatch_nested_mcp_async(registry, request, formatter, meta=meta, cancellable=cancellable)
        if cancellable:
            return asyncio.run(
                self._dispatch_nested_mcp_async(
                    registry,
                    request,
                    formatter,
                    meta=meta,
                    cancellable=True,
                )
            )

        with self._nested_mcp_scope(meta=meta, cancellable=False):
            response = registry.dispatch(request)
        if response is None:
            raise JsonRpcNoResponse()
        return formatter(response)

    async def _dispatch_nested_mcp_async(
        self,
        registry: JsonRpcRegistry,
        request: dict[str, Any],
        formatter: Callable[[Mapping[str, Any]], dict],
        *,
        meta: dict | None,
        cancellable: bool = False,
    ):
        with self._nested_mcp_scope(meta=meta, cancellable=cancellable) as cancellation:
            try:
                response = await registry.dispatch_async(request)
            except asyncio.CancelledError as exc:
                if cancellation is None or not cancellation.cancelled:
                    raise
                raise JsonRpcNoResponse() from exc
        if cancellation is not None and cancellation.cancelled:
            raise JsonRpcNoResponse()
        if response is None:
            raise JsonRpcNoResponse()
        return formatter(response)

    @contextmanager
    def _nested_mcp_scope(self, *, meta: dict | None, cancellable: bool = False):
        if cancellable:
            request_id, cancellation = self._register_cancellation()
        else:
            request_id = self.registry.current_request_id()
            cancellation = None

        try:
            with self._context_scope(request_id=request_id, meta=meta):
                yield cancellation
        finally:
            if cancellable:
                self._unregister_cancellation(request_id)

    def _register_cancellation(self) -> tuple[int | str | None, _McpCancellation | None]:
        request_id = self.registry.current_request_id()
        cancellation: _McpCancellation | None = None
        if request_id is not None:
            task = asyncio.current_task()
            if task is None:
                raise RuntimeError("async MCP cancellation requires an asyncio task")
            cancellation = _McpCancellation(asyncio.get_running_loop(), task)
            key = (self.get_current_transport_session_id(), request_id)
            with self._pending_requests_lock:
                self._pending_requests[key] = cancellation
        return request_id, cancellation

    def _unregister_cancellation(self, request_id: int | str | None) -> None:
        if request_id is not None:
            with self._pending_requests_lock:
                self._pending_requests.pop((self.get_current_transport_session_id(), request_id), None)

    def _current_protocol_version(self) -> str:
        return self.context.protocol_version or MCP_PROTOCOL_VERSION

    def _protocol_at_least(self, version: str) -> bool:
        # Protocol versions are ISO dates, so lexicographic order is chronological.
        return self._current_protocol_version() >= version

    def _tool_validation_errors_are_execution_errors(self) -> bool:
        return self._protocol_at_least("2025-11-25")

    def _format_tool_response(self, name: str, tool_response: Mapping[str, Any]) -> dict:
        if "error" in tool_response:
            error = tool_response["error"]
            code = error["code"]
            if code == -32800:
                raise JsonRpcNoResponse()
            if code == -32601 or (code == -32602 and not self._tool_validation_errors_are_execution_errors()):
                raise JsonRpcException(code, error["message"], error.get("data"))
            return {
                "content": [{"type": "text", "text": error["message"] or "Unknown error"}],
                "isError": True,
            }

        result = tool_response.get("result")
        content = result if isinstance(result, str) else json.dumps(result, indent=2)
        mcp_result = {
            "content": [{"type": "text", "text": content}],
            "isError": False,
        }
        structured_content = self._structured_content_for_tool(name, result)
        if structured_content is not None:
            mcp_result["structuredContent"] = structured_content
        return mcp_result

    def _mcp_notifications_initialized(self, _meta: dict | None = None) -> None:
        """MCP notifications/initialized method"""

    def _mcp_notifications_cancelled(self, requestId: int | str, reason: str | None = None, _meta: dict | None = None) -> None:
        """MCP notifications/cancelled method"""
        key = (self.get_current_transport_session_id(), requestId)
        with self._pending_requests_lock:
            cancellation = self._pending_requests.get(key)
            if cancellation is not None:
                cancellation.cancel(reason)

    def _structured_content_for_tool(self, name: str, result: Any) -> dict | None:
        func = self.tools.methods.get(name)
        if func is not None:
            return_type = get_type_hints(func, include_extras=True).get("return")
            if self._type_is_plain_str(return_type):
                return None
            if return_type and return_type is not type(None) and return_type is not Any:
                if not self._schema_is_object_like(self._type_to_json_schema(return_type)):
                    return {"result": result}
        return result if isinstance(result, dict) else {"result": result}

    def _enumerate_resources(self):
        for name, func in self.resources.methods.items():
            uri: str = getattr(func, "__resource_uri__")
            description = (func.__doc__ or f"Read {uri}").strip()
            yield uri, name, description

    def _mcp_resources_list(self, cursor: str | None = None, _meta: dict | None = None) -> dict:
        """MCP resources/list method - returns static resources only (no URI parameters)"""
        return {
            "resources": [
                {
                    "uri": uri,
                    "name": name,
                    "description": description,
                    "mimeType": "application/json",
                }
                for uri, name, description in self._enumerate_resources()
                if "{" not in uri
            ]
        }

    def _mcp_resource_templates_list(self, cursor: str | None = None, _meta: dict | None = None) -> dict:
        """MCP resources/templates/list method - returns parameterized resource templates"""
        return {
            "resourceTemplates": [
                {
                    "uriTemplate": uri,
                    "name": name,
                    "description": description,
                    "mimeType": "application/json",
                }
                for uri, name, description in self._enumerate_resources()
                if "{" in uri
            ]
        }

    def _mcp_resources_read(self, uri: str, _meta: dict | None = None):
        """MCP resources/read method"""
        match = self._match_resource(uri)
        if match is None:
            raise JsonRpcException(-32002, "Resource not found", {"uri": uri})

        name, params = match
        # Call the matched resource via JSON-RPC so argument validation stays shared.
        return self._dispatch_nested_mcp(
            self.resources,
            {
                "jsonrpc": "2.0",
                "method": name,
                "params": params,
                "id": 0,
            },
            lambda resource_response: self._format_resource_response(uri, resource_response),
            meta=_meta,
            cancellable=_is_async_callable(self.resources.methods.get(name)),
        )

    def _format_resource_response(self, uri: str, resource_response: Mapping[str, Any]) -> dict:
        # Check for error response.
        if "error" in resource_response:
            error = resource_response["error"]
            raise JsonRpcException(error["code"], error["message"], error.get("data"))

        return {
            "contents": [{
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(resource_response.get("result"), indent=2),
            }]
        }

    def _mcp_prompts_list(self, cursor: str | None = None, _meta: dict | None = None) -> dict:
        """MCP prompts/list method"""
        return {
            "prompts": [
                self._generate_prompt_schema(func_name, func)
                for func_name, func in self.prompts.methods.items()
            ],
        }

    def _mcp_prompts_get(
        self, name: str, arguments: dict | None = None, _meta: dict | None = None
    ):
        """MCP prompts/get method"""
        return self._dispatch_nested_mcp(
            self.prompts,
            {
                "jsonrpc": "2.0",
                "method": name,
                "params": arguments,
                "id": 0,
            },
            self._format_prompt_response,
            meta=_meta,
            cancellable=_is_async_callable(self.prompts.methods.get(name)),
        )

    def _format_prompt_response(self, prompt_response: Mapping[str, Any]) -> dict:
        # Check for error response.
        if "error" in prompt_response:
            error = prompt_response["error"]
            raise JsonRpcException(error["code"], error["message"], error.get("data"))

        result = prompt_response.get("result")

        # Pass through list of messages directly.
        if isinstance(result, list):
            return {"messages": result}

        # Convert non-string results to JSON.
        if not isinstance(result, str):
            result = json.dumps(result, indent=2)
        return {
            "messages": [
                {
                    "role": "user",
                    "content": {"type": "text", "text": result},
                },
            ],
        }

    def _generate_prompt_schema(self, func_name: str, func: Callable) -> dict:
        """Generate MCP prompt schema from a function"""
        hints = get_type_hints(func, include_extras=True)
        hints.pop("return", None)
        sig = inspect.signature(func)

        # Build arguments list (PromptArgument format)
        arguments = []
        for param_name, param_type in hints.items():
            arg: dict[str, Any] = {"name": param_name}

            # Extract description from Annotated
            origin = get_origin(param_type)
            if origin is Annotated:
                args = get_args(param_type)
                arg["description"] = str(args[-1])

            # Check if required (no default value)
            param = sig.parameters.get(param_name)
            if not param or param.default is inspect.Parameter.empty:
                arg["required"] = True

            arguments.append(arg)

        schema: dict[str, Any] = {
            "name": func_name,
            "description": (func.__doc__ or f"Prompt {func_name}").strip(),
        }

        if arguments:
            schema["arguments"] = arguments

        return schema

    def _schema_is_object_like(self, schema: dict) -> bool:
        if schema.get("type") == "object":
            return True
        if "anyOf" in schema:
            return all(self._schema_is_object_like(s) for s in schema["anyOf"])
        return False

    def _type_is_plain_str(self, py_type: Any) -> bool:
        if py_type is str:
            return True
        if get_origin(py_type) is Annotated:
            return self._type_is_plain_str(get_args(py_type)[0])
        return False

    def _type_to_json_schema(self, py_type: Any) -> dict:
        """Convert Python type hint to JSON schema object"""
        if py_type is Any:
            return {}

        origin = get_origin(py_type)
        # Annotated[T, "description"]
        if origin is Annotated:
            args = get_args(py_type)
            return {
                **self._type_to_json_schema(args[0]),
                "description": str(args[-1]),
            }

        # Required[T] / NotRequired[T]
        if origin in (Required, NotRequired):
            return self._type_to_json_schema(get_args(py_type)[0])

        # Union[Ts..], Optional[T] and T1 | T2
        if origin in (Union, UnionType):
            return {"anyOf": [self._type_to_json_schema(t) for t in get_args(py_type)]}

        # list[T]
        if origin is list:
            return {
                "type": "array",
                "items": self._type_to_json_schema(get_args(py_type)[0]),
            }

        # dict[str, T]
        if origin is dict:
            return {
                "type": "object",
                "additionalProperties": self._type_to_json_schema(get_args(py_type)[1]),
            }

        # TypedDict
        if is_typeddict(py_type):
            return self._typed_dict_to_schema(py_type)

        # Primitives
        return {
            "type": {
                int: "integer",
                float: "number",
                str: "string",
                bool: "boolean",
                list: "array",
                dict: "object",
                type(None): "null",
            }.get(py_type, "object"),
        }

    @staticmethod
    def _typeddict_field_qualifiers(annotation: Any) -> set:
        """Resolve Required[...]/NotRequired[...] wrappers on an already-evaluated annotation.

        Only meaningful once forward refs are resolved (e.g. via get_type_hints) - on a
        TypedDict class body, `Required`/`NotRequired` detection happens on the raw
        annotation at class-creation time, so it silently misses these wrappers when the
        defining module has `from __future__ import annotations` (the annotation is a bare
        string/ForwardRef at that point, not the real generic alias).
        """
        qualifiers = set()
        while True:
            origin = get_origin(annotation)
            if origin is Annotated:
                args = get_args(annotation)
                if not args:
                    break
                annotation = args[0]
            elif origin is Required:
                qualifiers.add(Required)
                (annotation,) = get_args(annotation)
            elif origin is NotRequired:
                qualifiers.add(NotRequired)
                (annotation,) = get_args(annotation)
            else:
                break
        return qualifiers

    def _typed_dict_to_schema(self, typed_dict_class) -> dict:
        """Convert TypedDict to JSON schema"""
        hints = get_type_hints(typed_dict_class, include_extras=True)
        required_keys = set(getattr(typed_dict_class, "__required_keys__", set(hints.keys())))
        optional_keys = set(getattr(typed_dict_class, "__optional_keys__", set()))

        # `__required_keys__`/`__optional_keys__` are computed by Python's TypedDict
        # metaclass at class-creation time by inspecting each field's raw annotation
        # for a Required[...]/NotRequired[...] wrapper. Under `from __future__ import
        # annotations`, that raw annotation is an unevaluated string/ForwardRef, so the
        # metaclass can't see the wrapper and silently falls back to the class's `total`
        # default. A field with no explicit wrapper is unaffected either way (there's
        # nothing to detect, so the `total` fallback the metaclass already applied is
        # correct) - so overriding only fields with an explicit, fully-resolved wrapper
        # here corrects the class's own (possibly wrong) computation unconditionally.
        for field_name, field_type in hints.items():
            qualifiers = self._typeddict_field_qualifiers(field_type)
            if Required in qualifiers:
                required_keys.add(field_name)
                optional_keys.discard(field_name)
            elif NotRequired in qualifiers:
                optional_keys.add(field_name)
                required_keys.discard(field_name)

        return {
            "type": "object",
            "properties": {
                field_name: self._type_to_json_schema(field_type)
                for field_name, field_type in hints.items()
            },
            "required": [key for key in hints.keys() if key in required_keys],
            "additionalProperties": False,
        }

    def _generate_tool_schema(self, func_name: str, func: Callable) -> dict:
        """Generate MCP tool schema from a function"""
        hints = get_type_hints(func, include_extras=True)
        return_type = hints.pop("return", None)
        sig = inspect.signature(func)

        # Build parameter schema
        properties = {}
        required = []

        for param_name, param_type in hints.items():
            properties[param_name] = self._type_to_json_schema(param_type)

            # Add to required if no default value
            param = sig.parameters.get(param_name)
            if not param or param.default is inspect.Parameter.empty:
                required.append(param_name)
            else:
                try:
                    json.dumps(param.default)
                except TypeError:
                    pass
                else:
                    properties[param_name]["default"] = param.default

        schema: dict[str, Any] = {
            "name": func_name,
            "description": (func.__doc__ or f"Call {func_name}").strip(),
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

        annotations = dict(getattr(func, "__mcp_tool_annotations__", None) or {})
        title = getattr(func, "__mcp_tool_title__", None)
        if title is not None:
            if self._protocol_at_least("2025-06-18"):
                schema["title"] = title
            else:
                annotations["title"] = title
        if annotations:
            schema["annotations"] = annotations

        # Add outputSchema if return type exists and is not None/Any/text-only.
        if return_type and return_type is not type(None) and return_type is not Any and not self._type_is_plain_str(return_type):
            return_schema = self._type_to_json_schema(return_type)

            # Wrap non-object returns in a "result" property.
            if not self._schema_is_object_like(return_schema):
                return_schema = {
                    "type": "object",
                    "properties": {"result": return_schema},
                    "required": ["result"],
                }
            elif return_schema.get("type") != "object":
                return_schema = {"type": "object", **return_schema}

            schema["outputSchema"] = return_schema

        return schema
