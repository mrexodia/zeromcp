import requests
import sys
import socket
from contextlib import contextmanager
from zeromcp import McpServer

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
        assert "Access-Control-Allow-Origin" not in resp.headers, "OPTIONS should NOT have CORS header"

        # Test POST
        resp = requests.post(f"{base_url}/mcp", headers={"Origin": "http://example.com"}, json=PING_JSON)
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
        assert "Access-Control-Allow-Origin" not in resp.headers, "OPTIONS should NOT have CORS header for wrong origin"

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
        assert "Access-Control-Allow-Origin" not in resp.headers, "OPTIONS should NOT have CORS header for disallowed origin"

        resp = requests.post(f"{base_url}/mcp", headers={"Origin": "http://notallowed.com"}, json=PING_JSON)
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
        test_cors_permissive()
        test_cors_restrictive()
        test_cors_local()
        test_cors_list()
        test_body_limit()
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
