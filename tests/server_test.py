import gzip
import io
import json
import requests
import sys
import socket
import zlib
from contextlib import contextmanager
from types import SimpleNamespace
from typing import BinaryIO, cast
from zeromcp import McpAuthInfo, McpServer, McpHttpRequestHandler

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

@contextmanager
def run_server(name="test", **kwargs):
    port = find_free_port()
    server = McpServer(name)
    for k, v in kwargs.items():
        setattr(server, k, v)
    server.serve("127.0.0.1", port, background=True)
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url, server
    finally:
        server.stop()

PING_JSON = {"jsonrpc": "2.0", "method": "ping", "id": 1}


def test_streamable_http_session_id():
    print("Testing Streamable HTTP session ID...")
    initialize = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
        "id": 1,
    }

    with run_server() as (base_url, server):
        resp = requests.post(f"{base_url}/mcp", json=initialize)
        session_id = resp.headers.get("Mcp-Session-Id")
        assert session_id, "initialize should return Mcp-Session-Id"
        assert not server.has_http_session(session_id), "server should not retain sessions unless enforcement is enabled"
        assert resp.json()["result"]["protocolVersion"] == "2024-11-05"

    with run_server(require_streamable_http_session=True) as (base_url, server):
        bad_session_id = "bad-init-session"
        resp = requests.post(
            f"{base_url}/mcp",
            headers={"Mcp-Session-Id": bad_session_id},
            json={"jsonrpc": "2.0", "method": "initialize", "id": 1},
        )
        assert "error" in resp.json(), "malformed initialize should fail"
        assert "Mcp-Session-Id" not in resp.headers, "failed initialize should not return a session ID"
        assert not server.has_http_session(bad_session_id), "failed initialize should not register session ID"
        resp = requests.post(f"{base_url}/mcp", headers={"Mcp-Session-Id": bad_session_id}, json=PING_JSON)
        assert resp.status_code == 404, "failed initialize session ID should not be accepted"

        resp = requests.post(f"{base_url}/mcp", json=initialize)
        session_id = resp.headers.get("Mcp-Session-Id")
        assert session_id, "initialize should return Mcp-Session-Id"
        assert server.has_http_session(session_id), "server should remember session ID when enforcement is enabled"
    print("✓ PASS")


def test_streamable_http_notifications_have_no_body():
    print("Testing Streamable HTTP notification response...")
    with run_server() as (base_url, _):
        resp = requests.post(f"{base_url}/mcp", json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        assert resp.status_code == 202
        assert resp.content == b""
    print("✓ PASS")


def test_streamable_http_session_id_is_server_generated():
    print("Testing server-generated Streamable HTTP session IDs...")
    initialize = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
        "id": 1,
    }

    with run_server(require_streamable_http_session=True) as (base_url, server):
        resp = requests.post(
            f"{base_url}/mcp",
            headers={"Mcp-Session-Id": "client-chosen-session"},
            json=initialize,
        )
        session_id = resp.headers.get("Mcp-Session-Id")
        assert session_id and session_id != "client-chosen-session", "server should generate initialize session IDs"
        assert server.has_http_session(session_id), "generated session should be registered"
        assert not server.has_http_session("client-chosen-session"), "client-provided initialize session should be ignored"

        bad = requests.post(f"{base_url}/mcp", headers={"Mcp-Session-Id": "client-chosen-session"}, json=PING_JSON)
        assert bad.status_code == 404, "client-chosen session ID should not be accepted"

        good = requests.post(f"{base_url}/mcp", headers={"Mcp-Session-Id": session_id}, json=PING_JSON)
        assert good.status_code == 200, "server-generated session ID should be accepted"
    print("✓ PASS")


def test_streamable_http_session_delete():
    print("Testing Streamable HTTP session termination...")
    initialize = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
        "id": 1,
    }

    with run_server(require_streamable_http_session=True) as (base_url, server):
        resp = requests.post(f"{base_url}/mcp", json=initialize)
        session_id = resp.headers.get("Mcp-Session-Id")
        assert session_id

        missing = requests.delete(f"{base_url}/mcp")
        assert missing.status_code == 400, "DELETE without Mcp-Session-Id should fail"

        unknown = requests.delete(f"{base_url}/mcp", headers={"Mcp-Session-Id": "unknown-session"})
        assert unknown.status_code == 404, "DELETE with an unknown session should fail"

        deleted = requests.delete(f"{base_url}/mcp", headers={"Mcp-Session-Id": session_id})
        assert deleted.status_code == 204, "DELETE with a known session should terminate it"
        assert not server.has_http_session(session_id)

        after = requests.post(f"{base_url}/mcp", headers={"Mcp-Session-Id": session_id}, json=PING_JSON)
        assert after.status_code == 404, "terminated sessions should not be accepted"

        other_path = requests.delete(f"{base_url}/sse", headers={"Mcp-Session-Id": session_id})
        assert other_path.status_code == 405, "DELETE is only supported on /mcp"

    # Without session enforcement, DELETE still discards the remembered protocol version.
    with run_server() as (base_url, server):
        resp = requests.post(f"{base_url}/mcp", json=initialize)
        session_id = resp.headers.get("Mcp-Session-Id")
        assert session_id
        assert server.get_http_session_protocol(session_id) == "2025-06-18"

        deleted = requests.delete(f"{base_url}/mcp", headers={"Mcp-Session-Id": session_id})
        assert deleted.status_code == 204
        assert server.get_http_session_protocol(session_id) is None

        again = requests.delete(f"{base_url}/mcp", headers={"Mcp-Session-Id": session_id})
        assert again.status_code == 404, "repeated DELETE should report the session as gone"
    print("✓ PASS")


def test_http_session_lru_bound():
    print("Testing HTTP session LRU bound...")
    initialize = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
        "id": 1,
    }

    with run_server(require_streamable_http_session=True, max_http_sessions=2) as (base_url, server):
        session_ids = []
        for i in range(3):
            resp = requests.post(f"{base_url}/mcp", json={**initialize, "id": i + 1})
            session_ids.append(resp.headers["Mcp-Session-Id"])

        assert not server.has_http_session(session_ids[0]), "oldest session should be evicted"
        assert server.has_http_session(session_ids[1])
        assert server.has_http_session(session_ids[2])

        evicted = requests.post(f"{base_url}/mcp", headers={"Mcp-Session-Id": session_ids[0]}, json=PING_JSON)
        assert evicted.status_code == 404, "evicted sessions should require re-initialization"
    print("✓ PASS")


def test_streamable_http_accepts_client_response():
    print("Testing Streamable HTTP client response input...")
    with run_server() as (base_url, _):
        resp = requests.post(f"{base_url}/mcp", json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {},
        })
        assert resp.status_code == 202
        assert resp.content == b""
    print("✓ PASS")


def test_protocol_version_header_validation():
    print("Testing protocol version header validation...")
    with run_server() as (base_url, _):
        for version in ("2025-03-26", "2025-06-18", "2025-11-25"):
            resp = requests.post(
                f"{base_url}/mcp",
                headers={"MCP-Protocol-Version": version},
                json=PING_JSON,
            )
            assert resp.status_code == 200, f"{version} should be accepted"

        # 2024-11-05 predates Streamable HTTP; it is served by /sse, not /mcp.
        for version in ("2024-11-05", "1900-01-01"):
            resp = requests.post(
                f"{base_url}/mcp",
                headers={"MCP-Protocol-Version": version},
                json=PING_JSON,
            )
            assert resp.status_code == 400, f"{version} should be rejected"
    print("✓ PASS")


def test_streamable_http_protocol_defaults_and_session_reuse():
    print("Testing Streamable HTTP protocol default and session protocol reuse...")
    initialize = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
        "id": 1,
    }

    with run_server() as (base_url, server):
        @server.tool
        def protocol_info() -> dict[str, str | None]:
            return {"protocol": server.context.protocol_version}

        no_header = requests.post(f"{base_url}/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "protocol_info", "arguments": {}},
            "id": 1,
        })
        assert no_header.status_code == 200
        no_header_result = no_header.json()["result"]
        assert no_header_result["structuredContent"]["protocol"] == "2025-03-26"
        assert json.loads(no_header_result["content"][0]["text"])["protocol"] == "2025-03-26"

        header_init = requests.post(
            f"{base_url}/mcp",
            headers={"MCP-Protocol-Version": "2025-03-26"},
            json=initialize,
        )
        assert header_init.json()["result"]["protocolVersion"] == "2025-11-25", "initialize should negotiate from the JSON-RPC body"

        init = requests.post(f"{base_url}/mcp", json=initialize)
        session_id = init.headers.get("Mcp-Session-Id")
        assert session_id
        followup = requests.post(
            f"{base_url}/mcp",
            headers={"Mcp-Session-Id": session_id},
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "protocol_info", "arguments": {}},
                "id": 2,
            },
        )
        assert followup.status_code == 200
        assert followup.json()["result"]["structuredContent"]["protocol"] == "2025-11-25"
    print("✓ PASS")


def test_tool_protocol_errors():
    print("Testing tool protocol errors...")
    with run_server() as (base_url, _):
        resp = requests.post(f"{base_url}/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "missing_tool", "arguments": {}},
            "id": 1,
        })
        data = resp.json()
        assert data["error"]["code"] == -32601
    print("✓ PASS")


def test_initialize_includes_server_instructions():
    print("Testing server instructions in initialize response...")
    request = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
        "id": 1,
    }

    instructions = "Use the search tool before answering questions."
    server = McpServer("instructions-test", instructions=instructions)
    response = server._dispatch_mcp(request)
    assert response is not None
    assert response["result"]["instructions"] == instructions

    server_without_instructions = McpServer("no-instructions-test")
    response = server_without_instructions._dispatch_mcp(request)
    assert response is not None
    assert "instructions" not in response["result"]
    print("✓ PASS")


def test_stdio_preserves_negotiated_protocol_version():
    print("Testing stdio negotiated protocol version...")
    server = McpServer("stdio-protocol-test")

    @server.tool
    def needs_arg(value: int) -> int:
        return value

    initialize = json.dumps({
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
        "id": 1,
    }).encode("utf-8") + b"\n"
    call = json.dumps({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "needs_arg", "arguments": {}},
        "id": 2,
    }).encode("utf-8") + b"\n"

    stdout = io.BytesIO()
    server.stdio(io.BytesIO(initialize + call), stdout)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]

    assert responses[0]["result"]["protocolVersion"] == "2025-11-25"
    tool_result = responses[1]["result"]
    assert tool_result["isError"]
    assert "missing required" in tool_result["content"][0]["text"]
    print("✓ PASS")


def test_protocol_specific_tool_argument_errors():
    print("Testing protocol-specific tool argument errors...")
    server = McpServer("tool-error-test")

    @server.tool
    def add(a: int, b: int) -> int:
        return a + b

    request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "add", "arguments": {"a": 1}},
        "id": 1,
    }

    with server._context_scope(protocol_version="2025-06-18"):
        old_response = server._dispatch_mcp(request)
    assert old_response is not None
    assert old_response["error"]["code"] == -32602, "2025-06-18 treats invalid tool args as protocol errors"

    with server._context_scope(protocol_version="2025-11-25"):
        new_response = server._dispatch_mcp(request)
    assert new_response is not None
    result = new_response["result"]
    assert result["isError"] is True, "2025-11-25 treats invalid tool args as tool execution errors"
    assert "missing required" in result["content"][0]["text"]
    print("✓ PASS")


def test_tool_schema_includes_future_fields_for_all_versions():
    print("Testing tool schema includes future fields for all protocol versions...")
    server = McpServer("schema-version-test")

    @server.tool(read_only=True)
    def number() -> int:
        return 1

    request = {"jsonrpc": "2.0", "method": "tools/list", "id": 1}

    for protocol_version in ("2024-11-05", "2025-03-26", "2025-06-18"):
        with server._context_scope(protocol_version=protocol_version):
            schema = server._dispatch_mcp(request)["result"]["tools"][0]
        assert schema["annotations"]["readOnlyHint"] is True
        assert schema["outputSchema"]["properties"]["result"]["type"] == "integer"
    print("✓ PASS")


def test_str_tool_result_is_unstructured_text():
    print("Testing string tool results are unstructured text...")
    server = McpServer("text-test")

    @server.tool
    def text_tool() -> str:
        return "hello \"world\"\nline2"

    list_response = server._dispatch_mcp({
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1,
    })
    assert list_response is not None
    tool_schema = list_response["result"]["tools"][0]
    assert "outputSchema" not in tool_schema, "str return tools should not advertise structured output"

    call_response = server._dispatch_mcp({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "text_tool", "arguments": {}},
        "id": 2,
    })
    assert call_response is not None
    result = call_response["result"]
    assert result["content"] == [{"type": "text", "text": "hello \"world\"\nline2"}]
    assert "structuredContent" not in result, "str return tools should not include structuredContent"
    print("✓ PASS")


def test_sync_tool_can_bridge_to_async_in_sync_transport():
    print("Testing sync tool stays outside the transport event loop...")
    import asyncio

    server = McpServer("sync-bridge-test")

    @server.tool
    def sync_bridge() -> str:
        return asyncio.run(asyncio.sleep(0, result="ok"))

    response = server._dispatch_mcp({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "sync_bridge", "arguments": {}},
        "id": 1,
    })
    assert response is not None
    result = response["result"]
    assert not result["isError"]
    assert result["content"][0]["text"] == "ok"
    print("✓ PASS")


def test_request_context_meta_and_async_tool():
    print("Testing request context metadata and async tools...")
    import asyncio

    server = McpServer("context-test")

    @server.tool
    async def inspect_context() -> dict:
        await asyncio.sleep(0)
        return {
            "request_id": server.context.request_id,
            "meta": server.context.meta,
        }

    response = server._dispatch_mcp({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {
            "name": "inspect_context",
            "arguments": {},
            "_meta": {"progressToken": "progress-1", "example/key": "value"},
        },
        "id": "ctx-id",
    })
    assert response is not None
    result = response["result"]
    assert not result["isError"]
    assert result["structuredContent"]["request_id"] == "ctx-id"
    assert result["structuredContent"]["meta"] == {"progressToken": "progress-1", "example/key": "value"}

    async def call_async_transport():
        return await server._dispatch_mcp_async({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "inspect_context",
                "arguments": {},
                "_meta": {"progressToken": "progress-2"},
            },
            "id": "ctx-async-id",
        })

    response = asyncio.run(call_async_transport())
    assert response is not None
    result = response["result"]
    assert result["structuredContent"]["request_id"] == "ctx-async-id"
    assert result["structuredContent"]["meta"] == {"progressToken": "progress-2"}
    print("✓ PASS")


def test_tool_annotations():
    print("Testing tool annotations...")
    server = McpServer("annotations-test")

    @server.tool(read_only=True, destructive=False, idempotent=True, open_world=False)
    def safe_tool() -> str:
        return "safe"

    response = server._dispatch_mcp({
        "jsonrpc": "2.0",
        "method": "tools/list",
        "id": 1,
    })
    assert response is not None
    tool_schema = response["result"]["tools"][0]
    assert tool_schema["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    print("✓ PASS")


def test_oauth_resource_server():
    print("Testing OAuth resource server support...")
    port = find_free_port()
    server = McpServer("oauth-test")

    @server.oauth(
        resource="http://resource.example/mcp",
        authorization_servers=["https://auth.example"],
        scopes_supported=["mcp"],
        required_scopes=["mcp"],
    )
    def verify_token(token: str, resource: str) -> McpAuthInfo | None:
        assert resource == "http://resource.example/mcp"
        if token == "good-token":
            return McpAuthInfo(subject="alice", scopes=frozenset({"mcp"}), claims={"sub": "alice"})
        if token == "no-scope-token":
            return McpAuthInfo(subject="bob", scopes=frozenset(), claims={"sub": "bob"})
        return None

    @server.tool
    def whoami() -> str:
        assert server.context.auth is not None
        return f"{server.context.auth.subject}:{server.context.auth.claims['sub']}"

    server.serve("127.0.0.1", port, background=True)
    base_url = f"http://127.0.0.1:{port}"
    try:
        metadata = requests.get(f"{base_url}/.well-known/oauth-protected-resource")
        assert metadata.status_code == 200
        assert metadata.json() == {
            "resource": "http://resource.example/mcp",
            "authorization_servers": ["https://auth.example"],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ["mcp"],
        }

        resp = requests.post(f"{base_url}/mcp", json=PING_JSON)
        assert resp.status_code == 401
        assert "WWW-Authenticate" in resp.headers
        assert "resource_metadata" in resp.headers["WWW-Authenticate"]

        resp = requests.post(f"{base_url}/mcp", headers={"Authorization": "Bearer bad-token"}, json=PING_JSON)
        assert resp.status_code == 401

        resp = requests.post(f"{base_url}/mcp", headers={"Authorization": "Bearer no-scope-token"}, json=PING_JSON)
        assert resp.status_code == 403

        resp = requests.options(
            f"{base_url}/mcp",
            headers={
                "Origin": "http://localhost:1234",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        assert "Authorization" in resp.headers.get("Access-Control-Allow-Headers", "")

        resp = requests.post(
            f"{base_url}/mcp",
            headers={"Authorization": "Bearer good-token"},
            json={
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
                "id": 1,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["result"]["content"][0]["text"] == "alice:alice"
    finally:
        server.stop()
    print("✓ PASS")


def test_list_cursor_params_are_accepted():
    print("Testing list cursor params...")
    with run_server() as (base_url, _):
        for method in ("tools/list", "resources/list", "resources/templates/list", "prompts/list"):
            resp = requests.post(f"{base_url}/mcp", json={
                "jsonrpc": "2.0",
                "method": method,
                "params": {"cursor": "ignored"},
                "id": 1,
            })
            assert resp.status_code == 200
            assert "result" in resp.json(), f"{method} should accept cursor"
    print("✓ PASS")


def test_resource_and_prompt_cancellation():
    print("Testing resource and prompt cancellation...")
    import asyncio
    import threading

    server = McpServer("cancel-non-tool-test")
    resource_started = threading.Event()
    prompt_started = threading.Event()

    @server.resource("example://slow")
    async def slow_resource():
        resource_started.set()
        await asyncio.Event().wait()

    @server.prompt
    async def slow_prompt():
        prompt_started.set()
        await asyncio.Event().wait()

    def run_and_cancel(request: dict, started: threading.Event):
        result = {}

        def call():
            result["response"] = server._dispatch_mcp(request)

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        assert started.wait(2), "request should have started"

        server._dispatch_mcp({
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": request["id"]},
        })
        thread.join(2)

        assert not thread.is_alive(), "request should stop after cancellation"
        assert result["response"] is None, "cancelled requests should not receive JSON-RPC responses"

    run_and_cancel({
        "jsonrpc": "2.0",
        "method": "resources/read",
        "params": {"uri": "example://slow"},
        "id": "resource-read",
    }, resource_started)
    run_and_cancel({
        "jsonrpc": "2.0",
        "method": "prompts/get",
        "params": {"name": "slow_prompt", "arguments": {}},
        "id": "prompt-get",
    }, prompt_started)
    print("✓ PASS")


def test_stdio_async_cancellation():
    print("Testing async stdio cancellation...")
    import asyncio
    import threading

    server = McpServer("stdio-cancel-test")
    started = threading.Event()

    @server.tool
    async def slow_tool():
        started.set()
        await asyncio.Event().wait()

    call = json.dumps({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "slow_tool", "arguments": {}},
        "id": 1,
    }).encode("utf-8") + b"\n"
    cancel = json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 1},
    }).encode("utf-8") + b"\n"

    class Stdin:
        def __init__(self):
            self.index = 0

        def readline(self):
            self.index += 1
            if self.index == 1:
                return call
            if self.index == 2:
                assert started.wait(2), "tool should start before cancellation is sent"
                return cancel
            return b""

    stdout = io.BytesIO()
    asyncio.run(server.stdio_async(cast(BinaryIO, Stdin()), stdout))
    assert stdout.getvalue() == b"", "cancelled requests should not receive JSON-RPC responses"
    print("✓ PASS")


def test_sync_tool_cancellation_is_ignored():
    print("Testing synchronous tool cancellation is ignored...")
    import threading

    server = McpServer("sync-cancel-test")
    started = threading.Event()
    release = threading.Event()
    result = {}

    @server.tool
    def slow_tool():
        started.set()
        assert release.wait(2)
        return "done"

    def call_tool():
        result["response"] = server._dispatch_mcp({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "slow_tool", "arguments": {}},
            "id": "sync-tool",
        })

    thread = threading.Thread(target=call_tool, daemon=True)
    thread.start()
    assert started.wait(2), "sync tool should have started"
    server._dispatch_mcp({
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": "sync-tool", "reason": "ignored"},
    })
    assert thread.is_alive(), "sync tool must not be interrupted"
    release.set()
    thread.join(2)

    assert result["response"]["result"]["isError"] is False
    print("✓ PASS")


def test_async_tool_controls_cancellation_cleanup():
    print("Testing async tool cancellation cleanup...")
    import asyncio
    import threading

    server = McpServer("async-cancel-test")
    started = threading.Event()
    observed = {}
    result = {}

    @server.tool
    async def slow_tool():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            observed["reason"] = error.args[0]
            observed["cleanup"] = True
            raise

    def call_tool():
        result["response"] = server._dispatch_mcp({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "slow_tool", "arguments": {}},
            "id": "async-tool",
        })

    thread = threading.Thread(target=call_tool, daemon=True)
    thread.start()
    assert started.wait(2), "async tool should have started"

    server._dispatch_mcp({
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": "async-tool", "reason": "client timeout"},
    })
    thread.join(2)

    assert not thread.is_alive(), "async tool should stop after cancellation"
    assert result["response"] is None
    assert observed == {"reason": "client timeout", "cleanup": True}
    print("✓ PASS")


def test_sync_wrapper_around_async_tool_stays_synchronous():
    print("Testing a synchronous wrapper around an async tool...")
    import asyncio
    from functools import wraps

    server = McpServer("sync-wrapped-async-test")

    def async_to_sync(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            return asyncio.run(func(*args, **kwargs))
        return wrapped

    @server.tool
    @async_to_sync
    async def wrapped_tool():
        await asyncio.sleep(0)
        return "done"

    response = server._dispatch_mcp({
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "wrapped_tool", "arguments": {}},
        "id": "wrapped-async-tool",
    })

    assert response is not None
    assert response["result"]["content"][0]["text"] == "done"
    print("✓ PASS")


def test_suppressed_async_cancellation_still_has_no_response():
    print("Testing suppressed async cancellation response handling...")
    import asyncio
    import threading

    server = McpServer("suppressed-cancel-test")
    started = threading.Event()
    result = {}

    @server.tool
    async def slow_tool():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return "suppressed"

    def call_tool():
        result["response"] = server._dispatch_mcp({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "slow_tool", "arguments": {}},
            "id": "suppressed",
        })

    thread = threading.Thread(target=call_tool, daemon=True)
    thread.start()
    assert started.wait(2), "async tool should have started"
    server._dispatch_mcp({
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": "suppressed"},
    })
    thread.join(2)

    assert not thread.is_alive()
    assert result["response"] is None
    print("✓ PASS")


def test_non_client_task_cancellation_propagates():
    print("Testing non-client task cancellation propagation...")
    import asyncio

    server = McpServer("owner-cancel-test")

    async def scenario():
        started = asyncio.Event()

        @server.tool
        async def slow_tool():
            started.set()
            await asyncio.Event().wait()

        request_task = asyncio.create_task(server._dispatch_mcp_async({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "slow_tool", "arguments": {}},
            "id": "owner-cancelled",
        }))
        await started.wait()
        request_task.cancel("server shutdown")
        try:
            await request_task
        except asyncio.CancelledError as error:
            assert error.args == ("server shutdown",)
        else:
            raise AssertionError("non-client cancellation should propagate")

    asyncio.run(scenario())
    assert not server._pending_requests
    print("✓ PASS")


def test_cancelled_async_tool_has_no_response():
    print("Testing cancelled async tool response suppression...")
    import asyncio
    import threading

    server = McpServer("cancel-test")
    started = threading.Event()
    result = {}

    @server.tool
    async def slow_tool():
        started.set()
        await asyncio.Event().wait()

    def call_tool():
        result["response"] = server._dispatch_mcp({
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "slow_tool", "arguments": {}},
            "id": 1,
        })

    thread = threading.Thread(target=call_tool, daemon=True)
    thread.start()
    assert started.wait(2), "tool should have started"

    server._dispatch_mcp({
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 1},
    })
    thread.join(2)

    assert not thread.is_alive(), "tool should stop after cancellation"
    assert result["response"] is None, "cancelled requests should not receive JSON-RPC responses"
    print("✓ PASS")


def test_anonymous_http_cancellation_is_isolated():
    print("Testing anonymous HTTP cancellation isolation...")
    import threading
    import time

    with run_server() as (base_url, server):
        started = threading.Event()
        release = threading.Event()
        result = {}

        @server.tool
        def slow_tool() -> str:
            started.set()
            release.wait(2)
            return "done"

        def call_tool():
            result["response"] = requests.post(
                f"{base_url}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {"name": "slow_tool", "arguments": {}},
                    "id": 1,
                },
                timeout=5,
            )

        thread = threading.Thread(target=call_tool, daemon=True)
        thread.start()
        try:
            assert started.wait(2), "tool should have started"
            cancel = requests.post(
                f"{base_url}/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 1},
                },
                timeout=5,
            )
            assert cancel.status_code == 202
            time.sleep(0.1)
            assert thread.is_alive(), "anonymous HTTP cancellation should not affect another request"
        finally:
            release.set()
            thread.join(2)

        assert not thread.is_alive(), "tool request should finish after release"
        response = result["response"]
        assert response.status_code == 200
        assert response.json()["result"]["content"][0]["text"] == "done"
    print("✓ PASS")


def test_cors_permissive():
    print("Testing CORS permissive (cors_allowed_origins='*')...")
    with run_server(cors_allowed_origins="*") as (base_url, _):
        test_origin = "http://example.com"
        # Test OPTIONS
        resp = requests.options(f"{base_url}/mcp", headers={"Origin": test_origin})
        assert resp.headers.get("Access-Control-Allow-Origin") == test_origin, "OPTIONS should have CORS header"

        # Test POST
        resp = requests.post(f"{base_url}/mcp", headers={"Origin": test_origin}, json=PING_JSON)
        assert resp.headers.get("Access-Control-Allow-Origin") == test_origin, "POST should have CORS header"
    print("✓ PASS")

def test_cors_restrictive():
    print("Testing CORS restrictive (cors_allowed_origins=None)...")
    with run_server(cors_allowed_origins=None) as (base_url, _):
        # Test OPTIONS
        resp = requests.options(f"{base_url}/mcp", headers={"Origin": "http://example.com"})
        assert resp.status_code == 403, "OPTIONS should reject disallowed origin"
        assert "Access-Control-Allow-Origin" not in resp.headers, "OPTIONS should NOT have CORS header"

        # Test POST
        resp = requests.post(f"{base_url}/mcp", headers={"Origin": "http://example.com"}, json=PING_JSON)
        assert resp.status_code == 403, "POST should reject disallowed origin"
        assert "Access-Control-Allow-Origin" not in resp.headers, "POST should NOT have CORS header"
    print("✓ PASS")

def test_cors_local():
    print("Testing CORS localhost...")
    with run_server() as (base_url, _):
        # Test OPTIONS
        resp = requests.options(f"{base_url}/mcp", headers={"Origin": "http://localhost:1234"})
        assert resp.headers.get("Access-Control-Allow-Origin") == "http://localhost:1234", "OPTIONS should have CORS header"

        # Test POST
        resp = requests.post(f"{base_url}/mcp", headers={"Origin": "https://127.0.0.1:4321"}, json=PING_JSON)
        assert resp.headers.get("Access-Control-Allow-Origin") == "https://127.0.0.1:4321", "POST should have CORS header (HTTPS)"

        resp = requests.post(f"{base_url}/mcp", headers={"Origin": "http://[::1]:4321"}, json=PING_JSON)
        assert resp.headers.get("Access-Control-Allow-Origin") == "http://[::1]:4321", "POST should have CORS header (IPv6)"

        # Test OPTIONS with wrong origin
        resp = requests.options(f"{base_url}/mcp", headers={"Origin": "http://example.com"})
        assert resp.status_code == 403, "OPTIONS should reject wrong origin"
        assert "Access-Control-Allow-Origin" not in resp.headers, "OPTIONS should NOT have CORS header for wrong origin"


def test_dns_rebinding_host_header():
    print("Testing DNS rebinding Host header guard...")
    with run_server() as (base_url, _):
        good = requests.post(f"{base_url}/mcp", headers={"Host": "localhost:1234"}, json=PING_JSON)
        assert good.status_code == 200, "loopback Host should be accepted"

        bad = requests.post(f"{base_url}/mcp", headers={"Host": "evil.example:1234"}, json=PING_JSON)
        assert bad.status_code == 403, "non-loopback Host should be rejected for loopback-bound servers"
        assert "Invalid Host" in bad.text
    print("✓ PASS")


def test_cors_list():
    print("Testing CORS list...")
    allowed_origins = ["http://example.com", "https://example.org"]
    with run_server(cors_allowed_origins=allowed_origins) as (base_url, _):
        # Test allowed origins
        for origin in allowed_origins:
            resp = requests.options(f"{base_url}/mcp", headers={"Origin": origin})
            assert resp.headers.get("Access-Control-Allow-Origin") == origin, f"OPTIONS should have CORS header for {origin}"

            resp = requests.post(f"{base_url}/mcp", headers={"Origin": origin}, json=PING_JSON)
            assert resp.headers.get("Access-Control-Allow-Origin") == origin, f"POST should have CORS header for {origin}"

        # Test disallowed origin
        resp = requests.options(f"{base_url}/mcp", headers={"Origin": "http://notallowed.com"})
        assert resp.status_code == 403, "OPTIONS should reject disallowed origin"
        assert "Access-Control-Allow-Origin" not in resp.headers, "OPTIONS should NOT have CORS header for disallowed origin"

        resp = requests.post(f"{base_url}/mcp", headers={"Origin": "http://notallowed.com"}, json=PING_JSON)
        assert resp.status_code == 403, "POST should reject disallowed origin"
        assert "Access-Control-Allow-Origin" not in resp.headers, "POST should NOT have CORS header for disallowed origin"


def test_body_limit():
    print("Testing body limit...")
    # Set small limit (100 bytes)
    with run_server(post_body_limit=100) as (base_url, _):
        # Small request - should pass
        resp = requests.post(f"{base_url}/mcp", json=PING_JSON)
        assert resp.status_code == 200, "Small request should pass"

        # Large request - should fail
        large_payload = "x" * 200
        resp = requests.post(f"{base_url}/mcp", data=large_payload)
        assert resp.status_code == 413, "Large request should fail with 413"
        assert "Payload Too Large" in resp.text, "Error message should mention payload size"
    print("✓ PASS")


def test_compressed_body_limit():
    print("Testing compressed body limit...")
    with run_server(post_body_limit=100) as (base_url, _):
        headers = {"Content-Encoding": "deflate", "Content-Type": "application/json"}
        small = zlib.compress(json.dumps(PING_JSON).encode("utf-8"))
        resp = requests.post(f"{base_url}/mcp", headers=headers, data=small)
        assert resp.status_code == 200, "Small compressed request should pass"

        compressed_bomb = zlib.compress(b"x" * 1000)
        resp = requests.post(f"{base_url}/mcp", headers=headers, data=compressed_bomb)
        assert resp.status_code == 413, "Compressed payload should fail after exceeding decompressed limit"
        assert "Payload Too Large" in resp.text, "Error message should mention payload size"
    print("✓ PASS")


def test_concatenated_gzip_body_limit():
    print("Testing concatenated gzip body limit...")
    with run_server(post_body_limit=100) as (base_url, _):
        headers = {"Content-Encoding": "gzip", "Content-Type": "application/json"}
        first_member = gzip.compress(json.dumps(PING_JSON).encode("utf-8"))
        second_member = gzip.compress(b"x" * 1000)
        resp = requests.post(f"{base_url}/mcp", headers=headers, data=first_member + second_member)
        assert resp.status_code == 413, "Concatenated gzip members should count against decompressed limit"
        assert "Payload Too Large" in resp.text, "Error message should mention payload size"
    print("✓ PASS")


def test_content_length_overlimit_closes_connection():
    print("Testing Content-Length overlimit connection close...")
    handler = object.__new__(McpHttpRequestHandler)
    handler.mcp_server = SimpleNamespace(post_body_limit=5)
    handler.headers = {"Content-Length": "10"}
    handler.rfile = io.BytesIO(b"abcdefghijGET /mcp HTTP/1.1\r\n\r\n")
    handler.close_connection = False
    errors = []
    handler.send_error = lambda code, message=None, explain=None: errors.append((code, message))

    body = handler._read_body()

    assert body is None, "Over-limit Content-Length body should be rejected"
    assert handler.close_connection is True, "Over-limit Content-Length should close the connection"
    assert errors and errors[0][0] == 413, "Over-limit Content-Length should send 413"
    assert handler.rfile.read().startswith(b"abcdefghij"), "Over-limit Content-Length body should not need draining"
    print("✓ PASS")


def test_chunked_overlimit_closes_connection():
    print("Testing chunked overlimit connection close...")
    handler = object.__new__(McpHttpRequestHandler)
    handler.mcp_server = SimpleNamespace(post_body_limit=5)
    handler.headers = {"Transfer-Encoding": "chunked"}
    handler.rfile = io.BytesIO(b"a\r\nabcdefghij\r\n0\r\n\r\nGET /mcp HTTP/1.1\r\n\r\n")
    handler.close_connection = False
    errors = []
    handler.send_error = lambda code, message=None, explain=None: errors.append((code, message))

    body = handler._read_body()

    assert body is None, "Over-limit chunked body should be rejected"
    assert handler.close_connection is True, "Over-limit chunked body should close the connection"
    assert errors and errors[0][0] == 413, "Over-limit chunked body should send 413"
    assert handler.rfile.read().startswith(b"abcdefghij"), "Over-limit chunked body should not need draining"
    print("✓ PASS")


def test_exception_redaction():
    print("Testing exception redaction...")
    with run_server() as (base_url, server):
        server.tools.redact_exceptions = True

        @server.tool
        def fail():
            raise ValueError("Secret internal info")

        # Call via tools/call
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "fail", "arguments": {}},
            "id": 1
        }
        resp = requests.post(f"{base_url}/mcp", json=payload)
        data = resp.json()

        # The outer JSON-RPC call succeeds
        assert "result" in data, f"Expected result, got error: {data.get('error')}"
        result = data["result"]

        # The tool execution failed
        assert result["isError"] is True, "Tool execution should be an error"
        error_text = result["content"][0]["text"]

        assert error_text == "Internal Error: Secret internal info", f"Should show redacted message, got: {error_text}"
        assert "Traceback" not in error_text, "Should NOT show traceback"
    print("✓ PASS")

def test_exception_exposure():
    print("Testing exception exposure (default)...")
    with run_server() as (base_url, server):
        server.tools.redact_exceptions = False

        @server.tool
        def fail():
            raise ValueError("Secret internal info")

        # Call via tools/call
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "fail", "arguments": {}},
            "id": 1
        }
        resp = requests.post(f"{base_url}/mcp", json=payload)
        data = resp.json()

        # The outer JSON-RPC call succeeds
        assert "result" in data, f"Expected result, got error: {data.get('error')}"
        result = data["result"]

        # The tool execution failed
        assert result["isError"] is True, "Tool execution should be an error"
        error_text = result["content"][0]["text"]

        assert "Secret internal info" in error_text, "Should show exception message"
        assert "Traceback" in error_text, "Should show traceback"
    print("✓ PASS")

    print("✓ PASS")

def test_http_errors():
    print("Testing HTTP errors...")
    with run_server() as (base_url, _):
        # GET /mcp -> 405 Method Not Allowed
        resp = requests.get(f"{base_url}/mcp")
        assert resp.status_code == 405, f"GET /mcp should return 405, got {resp.status_code}"

        # GET /invalid -> 404 Not Found
        resp = requests.get(f"{base_url}/invalid")
        assert resp.status_code == 404, f"GET /invalid should return 404, got {resp.status_code}"

        # POST /invalid -> 404 Not Found
        resp = requests.post(f"{base_url}/invalid", json={})
        assert resp.status_code == 404, f"POST /invalid should return 404, got {resp.status_code}"
    print("✓ PASS")

def test_sse_errors():
    print("Testing SSE errors...")
    with run_server() as (base_url, _):
        # POST /sse without session -> 400 Bad Request
        resp = requests.post(f"{base_url}/sse", json={})
        assert resp.status_code == 400, f"POST /sse without session should return 400, got {resp.status_code}"
        assert "Missing ?session" in resp.text

        # POST /sse with invalid session -> 400 Bad Request
        resp = requests.post(f"{base_url}/sse?session=invalid-uuid", json={})
        assert resp.status_code == 400, f"POST /sse with invalid session should return 400, got {resp.status_code}"
        assert "No active SSE connection" in resp.text
    print("✓ PASS")

def test_mcp_tool_error():
    print("Testing McpToolError...")
    from zeromcp import McpToolError
    with run_server() as (base_url, server):
        @server.tool
        def fail_custom():
            raise McpToolError("Custom tool error")

        resp = requests.post(f"{base_url}/mcp", json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "fail_custom", "arguments": {}},
            "id": 1
        })
        data = resp.json()
        result = data["result"]
        assert result["isError"] is True
        assert "Custom tool error" in result["content"][0]["text"]
    print("✓ PASS")

def run_all_tests():
    print("="*60)
    print("SERVER TESTS")
    print("="*60)

    try:
        test_streamable_http_session_id()
        test_streamable_http_notifications_have_no_body()
        test_streamable_http_session_id_is_server_generated()
        test_streamable_http_session_delete()
        test_http_session_lru_bound()
        test_streamable_http_accepts_client_response()
        test_protocol_version_header_validation()
        test_streamable_http_protocol_defaults_and_session_reuse()
        test_tool_protocol_errors()
        test_initialize_includes_server_instructions()
        test_stdio_preserves_negotiated_protocol_version()
        test_protocol_specific_tool_argument_errors()
        test_tool_schema_includes_future_fields_for_all_versions()
        test_str_tool_result_is_unstructured_text()
        test_sync_tool_can_bridge_to_async_in_sync_transport()
        test_request_context_meta_and_async_tool()
        test_tool_annotations()
        test_oauth_resource_server()
        test_list_cursor_params_are_accepted()
        test_resource_and_prompt_cancellation()
        test_stdio_async_cancellation()
        test_sync_tool_cancellation_is_ignored()
        test_async_tool_controls_cancellation_cleanup()
        test_sync_wrapper_around_async_tool_stays_synchronous()
        test_suppressed_async_cancellation_still_has_no_response()
        test_non_client_task_cancellation_propagates()
        test_cancelled_async_tool_has_no_response()
        test_anonymous_http_cancellation_is_isolated()
        test_cors_permissive()
        test_cors_restrictive()
        test_cors_local()
        test_dns_rebinding_host_header()
        test_cors_list()
        test_body_limit()
        test_content_length_overlimit_closes_connection()
        test_compressed_body_limit()
        test_concatenated_gzip_body_limit()
        test_chunked_overlimit_closes_connection()
        test_exception_redaction()
        test_exception_exposure()
        test_http_errors()
        test_sse_errors()
        test_mcp_tool_error()
        print("\n" + "="*60)
        print("ALL SERVER TESTS PASSED! ✓")
        print("="*60)
    except AssertionError as e:
        print(f"\n❌ FAIL: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    run_all_tests()
