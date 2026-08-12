"""Microbenchmark the in-process JSON-RPC and MCP dispatch paths."""

import json
import statistics
import timeit

from zeromcp import McpServer
from zeromcp.jsonrpc import JsonRpcRegistry


def measure(statement: str, *, number: int, scope: dict) -> float:
    runs = timeit.repeat(statement, number=number, repeat=9, globals=scope)
    return statistics.median(runs) / number * 1e9


def main() -> None:
    registry = JsonRpcRegistry()

    @registry.method
    def add(a: int, b: int) -> int:
        return a + b

    rpc_request = {
        "jsonrpc": "2.0",
        "method": "add",
        "params": {"a": 1, "b": 2},
        "id": 1,
    }

    server = McpServer("benchmark")

    @server.tool
    def tool_add(a: int, b: int) -> int:
        return a + b

    mcp_request = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "tool_add", "arguments": {"a": 1, "b": 2}},
        "id": 1,
    }

    rpc_json = json.dumps(rpc_request)
    mcp_json = json.dumps(mcp_request)

    # Warm lazy caches before timing steady-state dispatch.
    registry.dispatch(rpc_request)
    server._dispatch_mcp(mcp_request)

    scope = locals()
    results = {
        "direct function": measure("add(1, 2)", number=1_000_000, scope=scope),
        "JSON-RPC dict": measure("registry.dispatch(rpc_request)", number=100_000, scope=scope),
        "JSON-RPC JSON": measure("registry.dispatch(rpc_json)", number=100_000, scope=scope),
        "MCP dict": measure("server._dispatch_mcp(mcp_request)", number=20_000, scope=scope),
        "MCP JSON": measure("server._dispatch_mcp(mcp_json)", number=20_000, scope=scope),
    }
    for label, nanoseconds in results.items():
        print(f"{label:20s} {nanoseconds:9.1f} ns/call  {1e9 / nanoseconds:10,.0f} calls/s")


if __name__ == "__main__":
    main()
