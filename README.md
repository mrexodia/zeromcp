# zeromcp

**Minimal MCP server implementation in pure Python.**

A lightweight, handcrafted implementation of the [Model Context Protocol](https://modelcontextprotocol.io/) focused on what most users actually need: exposing tools with clean Python type annotations.

## Features

- ✨ **Zero dependencies** - Pure Python, standard library only
- 🎯 **Type-safe** - Native Python type annotations for everything
- 🚀 **Fast** - Minimal overhead, maximum performance
- 🛠️ **Handcrafted** - Written by a human, verified against the spec
- 🌐 **HTTP/SSE transport** - Streamable responses (stdio planned)
- 📦 **Tiny** - Less than 1,000 lines of code

## Installation

```bash
pip install zeromcp
```

Or with uv:

```bash
uv add zeromcp
```

## Quick Start

```python
from typing import Annotated
from zeromcp import McpServer

mcp = McpServer("my-server")

@mcp.tool
def greet(
    name: Annotated[str, "Name to greet"],
    age: Annotated[int | None, "Age of person"] = None
) -> str:
    """Generate a greeting message"""
    if age:
        return f"Hello, {name}! You are {age} years old."
    return f"Hello, {name}!"

if __name__ == "__main__":
    mcp.start("127.0.0.1", 8000)
```

Then manually test your MCP server with the [inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx -y @modelcontextprotocol/inspector
```

Once things are working you can configure the `mcp.json`:

```json
{
  "mcpServers": {
    "my-server": {
      "type": "http",
      "url": "http://127.0.0.1/mcp"
    }
  }
}
```

## Type Annotations

zeromcp uses native Python `Annotated` types for schema generation:

```python
from typing import Annotated, Optional, TypedDict, NotRequired

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
```

## Union Types

Tools can accept multiple input types:

```python
from typing import Annotated, TypedDict

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
        {
            "name": name,
            "size": 128,
            "fields": ["field1", "field2", "field3"]
        }
        for name in (names if isinstance(names, list) else [names])
    ]
```

## Error Handling

```python
from zeromcp import McpToolError

@mcp.tool
def divide(
    numerator: Annotated[float, "Numerator"],
    denominator: Annotated[float, "Denominator"]
) -> float:
    """Divide two numbers"""
    if denominator == 0:
        raise McpToolError("Division by zero")
    return numerator / denominator
```
