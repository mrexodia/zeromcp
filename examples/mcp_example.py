"""Example MCP server with test tools"""
import time
import argparse
from urllib.parse import urlparse
from typing import Annotated, Optional, TypedDict, NotRequired
from zeromcp import McpToolError, McpServer

mcp = McpServer("example")

@mcp.tool
def divide(
    numerator: Annotated[float, "Numerator"],
    denominator: Annotated[float, "Denominator"]
) -> float:
    """Divide two numbers (no zero check - tests natural exceptions)"""
    return numerator / denominator

class GreetingResponse(TypedDict):
    message: Annotated[str, "Greeting message"]
    name: Annotated[str, "Name that was greeted"]
    age: Annotated[NotRequired[int], "Age if provided"]

@mcp.tool
def greet(
    name: Annotated[str, "Name to greet"],
    age: Annotated[Optional[int], "Age of person"] = None
) -> GreetingResponse:
    """Generate a greeting message"""
    if age is not None:
        return {
            "message": f"Hello, {name}! You are {age} years old.",
            "name": name,
            "age": age
        }
    return {
        "message": f"Hello, {name}!",
        "name": name
    }

class SystemInfo(TypedDict):
    platform: Annotated[str, "Operating system platform"]
    python_version: Annotated[str, "Python version"]
    machine: Annotated[str, "Machine architecture"]
    timestamp: Annotated[float, "Current timestamp"]

@mcp.tool
def get_system_info() -> SystemInfo:
    """Get system information"""
    import platform
    return {
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "machine": platform.machine(),
        "timestamp": time.time()
    }

@mcp.tool
def failing_tool(message: Annotated[str, "Error message to raise"]) -> str:
    """Tool that always fails (for testing error handling)"""
    raise McpToolError(message)

class StructInfo(TypedDict):
    name: Annotated[str, "Structure name"]
    size: Annotated[int, "Structure size in bytes"]
    fields: Annotated[list[str], "List of field names"]

@mcp.tool
def struct_get(
    names: Annotated[list[str], "Array of structure names"]
         | Annotated[str, "Single structure name"]
) -> list[StructInfo]:
    """Retrieve structure information by names"""
    return [
        StructInfo({
            "name": name,
            "size": 128,  # Dummy size
            "fields": ["field1", "field2", "field3"]  # Dummy fields
        })
        for name in (names if isinstance(names, list) else [names])
    ]

@mcp.tool
def random_dict(param: dict[str, int] | None) -> dict:
    """Return a random dictionary for testing serialization"""
    return {
        **(param or {}),
        "x": 42,
        "y": 7,
        "z": 99,
    }

@mcp.resource("example://system_info")
def system_info_resource() -> SystemInfo:
    """Resource providing system information"""
    return get_system_info()

@mcp.resource("example://greeting/{name}")
def greeting_resource(
    name: Annotated[str, "Name to greet from resource"]
) -> GreetingResponse:
    """Resource providing greeting message"""
    return greet(name)

@mcp.resource("example://error")
def error_resource() -> None:
    """Resource that always fails (for testing error handling)"""
    raise McpToolError("This is a resource error for testing purposes.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP Example Server")
    parser.add_argument("--transport", help="Transport (stdio or http://host:port)", default="http://127.0.0.1:5001")
    args = parser.parse_args()
    if args.transport == "stdio":
        mcp.stdio()
    else:
        url = urlparse(args.transport)
        if url.hostname is None or url.port is None:
            raise Exception(f"Invalid transport URL: {args.transport}")

        print("Starting MCP Example Server...")
        print("\nAvailable tools:")
        for name in mcp.tools.methods.keys():
            func = mcp.tools.methods[name]
            print(f"  - {name}: {func.__doc__}")

        mcp.serve(url.hostname, url.port)

        try:
            input("Server is running, press Enter or Ctrl+C to stop.")
        except (KeyboardInterrupt, EOFError):
            print("\n\nStopping server...")
            mcp.stop()
