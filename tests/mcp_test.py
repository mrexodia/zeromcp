import os
import sys
import socket
import asyncio
import subprocess

from pydantic import AnyUrl
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

example_mcp = os.path.join(os.path.dirname(__file__), "..", "examples", "mcp_example.py")
assert os.path.exists(example_mcp), f"not found: {example_mcp}"

async def test_example_server(prefix: str, session: ClientSession):
    # Initialize the connection
    await session.initialize()

    # Test ping
    ping_result = await session.send_ping()
    print(f"[{prefix}] Ping result: {ping_result}")
    assert isinstance(ping_result, types.EmptyResult), "ping should return EmptyResult"

    # List available resources
    resources = await session.list_resources()
    print(f"[{prefix}] Available resources: {[r.uri for r in resources.resources]}")
    assert len(resources.resources) == 2, "expected 2 static resources"
    assert str(resources.resources[0].uri) == "example://system_info", "expected system_info resource"

    # List available resource templates
    template_resources = await session.list_resource_templates()
    print(f"[{prefix}] Available resource templates: {[r.uriTemplate for r in template_resources.resourceTemplates]}")
    assert len(template_resources.resourceTemplates) == 1, "expected 1 resource template"
    assert template_resources.resourceTemplates[0].uriTemplate == "example://greeting/{name}", "expected greeting template"

    # List available tools
    tools = await session.list_tools()
    print(f"[{prefix}] Available tools: {[t.name for t in tools.tools]}")
    tool_names = {t.name for t in tools.tools}
    assert tool_names == {"divide", "greet", "random_dict", "get_system_info", "failing_tool", "struct_get"}, f"unexpected tools: {tool_names}"

    # Read a resource - assert content
    resource_content = await session.read_resource(AnyUrl("example://system_info"))
    content_block = resource_content.contents[0]
    assert isinstance(content_block, types.TextResourceContents), "expected TextResourceContents"
    print(f"[{prefix}] Resource content: {content_block.text}")
    assert "platform" in content_block.text, "expected platform in system_info"
    assert "python_version" in content_block.text, "expected python_version in system_info"

    # Read template resource - assert content
    template_content = await session.read_resource(AnyUrl("example://greeting/Pythonista"))
    template_block = template_content.contents[0]
    assert isinstance(template_block, types.TextResourceContents), "expected TextResourceContents"
    print(f"[{prefix}] Template resource content: {template_block.text}")
    assert "Pythonista" in template_block.text, "expected name in greeting"
    assert "message" in template_block.text, "expected message field in greeting"

    # Call divide tool
    result = await session.call_tool("divide", arguments={"numerator": 42, "denominator": 2})
    assert not result.isError, "divide should succeed"
    result_unstructured = result.content[0]
    assert isinstance(result_unstructured, types.TextContent), "expected TextContent"
    print(f"[{prefix}] Divide result: {result_unstructured.text}")
    assert "21" in result_unstructured.text, "42/2 should be 21"

    # Call greet tool without age
    result = await session.call_tool("greet", arguments={"name": "Alice"})
    assert not result.isError, "greet should succeed"
    assert isinstance(result.content[0], types.TextContent), "expected TextContent"
    content = result.content[0].text
    print(f"[{prefix}] Greet result: {content}")
    assert "Alice" in content, "expected name in greeting"
    assert "message" in content, "expected message field"
    assert "age" not in content or content.count("age") == 1, "age should not have value"

    # Call greet tool with age
    result = await session.call_tool("greet", arguments={"name": "Bob", "age": 30})
    assert not result.isError, "greet with age should succeed"
    assert isinstance(result.content[0], types.TextContent), "expected TextContent"
    content = result.content[0].text
    print(f"[{prefix}] Greet with age result: {content}")
    assert "Bob" in content and "30" in content, "expected name and age"

    # Call get_system_info tool
    result = await session.call_tool("get_system_info", arguments={})
    assert not result.isError, "get_system_info should succeed"
    assert isinstance(result.content[0], types.TextContent), "expected TextContent"
    content = result.content[0].text
    print(f"[{prefix}] System info result: {content}")
    assert "platform" in content, "expected platform"
    assert "python_version" in content, "expected python_version"
    assert "timestamp" in content, "expected timestamp"

    # Call struct_get with list
    result = await session.call_tool("struct_get", arguments={"names": ["foo", "bar"]})
    assert not result.isError, "struct_get with list should succeed"
    assert isinstance(result.content[0], types.TextContent), "expected TextContent"
    content = result.content[0].text
    print(f"[{prefix}] Struct_get (list) result: {content}")
    assert "foo" in content and "bar" in content, "expected both struct names"

    # Call struct_get with string
    result = await session.call_tool("struct_get", arguments={"names": "baz"})
    assert not result.isError, "struct_get with string should succeed"
    assert isinstance(result.content[0], types.TextContent), "expected TextContent"
    content = result.content[0].text
    print(f"[{prefix}] Struct_get (string) result: {content}")
    assert "baz" in content, "expected struct name"

    # Call failing tool
    result = await session.call_tool("failing_tool", arguments={"message": "This is a test error"})
    print(f"[{prefix}] Failing tool result: {result}")
    assert result.isError, "expected tool call to fail"
    assert isinstance(result.content[0], types.TextContent), "expected text content in tool call result"
    assert "test error" in result.content[0].text, "expected error message in tool call result"

    # Call random_dict tool
    result = await session.call_tool("random_dict", arguments={"param": {"x": 112, "other": "yes"}})
    assert not result.isError, "random_dict should succeed"
    assert isinstance(result.content[0], types.TextContent), "expected TextContent"
    content = result.content[0].text
    print(f"[{prefix}] Random dict result: {content}")

    # Call random_dict tool with null
    result = await session.call_tool("random_dict", arguments={"param": None})
    assert not result.isError, "random_dict with null should succeed"

async def test_edge_cases(prefix: str, session: ClientSession):
    """Test edge cases and error conditions"""
    await session.initialize()

    # Test non-existent tool
    result = await session.call_tool("nonexistent_tool", arguments={})
    assert result.isError, "should error on non-existent tool"
    print(f"[{prefix}] Non-existent tool error: {result.content[0] if result.content else 'no content'}")

    # Test missing required parameter
    result = await session.call_tool("divide", arguments={"numerator": 42})
    assert result.isError, "should error on missing denominator"
    print(f"[{prefix}] Missing param error: {result.content[0] if result.content else 'no content'}")

    # Test division by zero (natural exception)
    result = await session.call_tool("divide", arguments={"numerator": 1, "denominator": 0})
    assert result.isError, "division by zero should error"
    print(f"[{prefix}] Division by zero error: {result.content[0] if result.content else 'no content'}")

    # Test invalid resource URI
    result = await session.read_resource(AnyUrl("example://invalid_resource"))
    assert hasattr(result, "isError") and result.isError, "should error on invalid resource"  # type: ignore
    content = result.contents[0]  # type: ignore
    if isinstance(content, types.TextResourceContents):
        print(f"[{prefix}] Invalid resource error: {content.text}")

    # Test resource template with missing substitution
    result = await session.read_resource(AnyUrl("example://greeting/"))
    assert hasattr(result, "isError") and result.isError, "should error on malformed template URI"  # type: ignore
    content = result.contents[0]  # type: ignore
    if isinstance(content, types.TextResourceContents):
        print(f"[{prefix}] Malformed template URI error: {content.text}")

    # Test resource that raises an error
    result = await session.read_resource(AnyUrl("example://error"))
    assert hasattr(result, "isError") and result.isError, "should error on error resource"  # type: ignore
    content = result.contents[0]  # type: ignore
    if isinstance(content, types.TextResourceContents):
        print(f"[{prefix}] Error resource error: {content.text}")

def coverage_wrap(name: str, args: list[str]) -> list[str]:
    if os.environ.get("COVERAGE_RUN"):
        args = ["-m", "coverage", "run", f"--data-file=.coverage.{name}"] + args
    return args

async def test_stdio():
    print("[stdio] Testing...")
    server_params = StdioServerParameters(
        command=sys.executable,
        args=coverage_wrap("stdio", [example_mcp, "--transport", "stdio"]),
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await test_example_server("stdio", session)
            await test_edge_cases("stdio", session)

async def test_sse(address: str):
    print("[sse] Testing...")
    async with sse_client(f"{address}/sse") as (read, write):
        async with ClientSession(read, write) as session:
            await test_example_server("sse", session)
            await test_edge_cases("sse", session)

async def test_streamablehttp(address: str):
    print("[streamable] Testing...")
    async with streamablehttp_client(f"{address}/mcp") as (read, write, session_callback):
        async with ClientSession(read, write) as session:
            await test_example_server("streamable", session)
            await test_edge_cases("streamable", session)

def find_available_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port

async def test_serve():
    print("[serve] Testing...")

    # Start example MCP server as subprocess
    address = f"http://127.0.0.1:{find_available_port()}"
    process = subprocess.Popen(
        [sys.executable] + coverage_wrap("serve", [example_mcp, "--transport", address]),
        stdin=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    try:
        await test_sse(address)
        await test_streamablehttp(address)
    finally:
        print("[serve] Terminating example MCP server")
        process.stdin.close() # type: ignore
        process.wait()
    pass

async def main():
    await test_stdio()
    await test_serve()

if __name__ == "__main__":
    import os
    asyncio.run(main())
