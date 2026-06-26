# zeromcp

**Minimal MCP server implementation in pure Python.**

A lightweight, handcrafted implementation of the [Model Context Protocol](https://modelcontextprotocol.io/) focused on what most users actually need: exposing tools with clean Python type annotations.

## Features

- ✨ **Zero dependencies** - Pure Python, standard library only
- 🎯 **Type-safe** - Native Python type annotations for everything
- 🚀 **Fast** - Minimal overhead, maximum performance
- 🛠️ **Handcrafted** - Written by a human<sup>[1](#ai-usage)</sup>, verified against the spec
- 🌐 **HTTP/SSE transport** - Streamable responses
- 📡 **Stdio transport** - For legacy clients
- 📦 **Tiny** - Compact codebase with no framework dependency

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
    mcp.serve("127.0.0.1", 8000)
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
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

## Stdio Transport

For MCP clients that only support stdio transport:

```python
from zeromcp import McpServer

mcp = McpServer("my-server")

@mcp.tool
def greet(name: str) -> str:
    """Generate a greeting"""
    return f"Hello, {name}!"

if __name__ == "__main__":
    mcp.stdio()
```

Then configure in `mcp.json` (different for every client):

```json
{
  "mcpServers": {
    "my-server": {
      "command": "python",
      "args": ["path/to/server.py"]
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

## Resources

Expose read-only data via URI patterns. Resources are serialized as JSON.

```python
from typing import Annotated

@mcp.resource("file://data.txt")
def read_file() -> dict:
    """Get information about data.txt"""
    return {"name": "data.txt", "size": 1024}

@mcp.resource("file://{filename}")
def read_any_file(
    filename: Annotated[str, "Name of file to read"]
) -> dict:
    """Get information about any file"""
    return {"name": filename, "size": 2048}
```

## Prompts

Expose reusable prompt templates with typed arguments.

```python
from typing import Annotated

@mcp.prompt
def code_review(
    code: Annotated[str, "Code to review"],
    language: Annotated[str, "Programming language"] = "python"
) -> str:
    """Review code for bugs and improvements"""
    return f"Please review this {language} code:\n\n```{language}\n{code}\n```"
```

## Tool annotations

MCP 2025-03-26+ supports tool behavior hints:

```python
@mcp.tool(read_only=True, destructive=False, idempotent=True, open_world=False)
def get_status() -> dict:
    """Read current system status"""
    return {"ok": True}
```

## Request context

Tools, resources, and prompts can inspect the current MCP request context:

```python
@mcp.tool
def inspect_request() -> dict:
    return {
        "request_id": mcp.context.request_id,
        "meta": mcp.context.meta,
        "auth_subject": mcp.context.auth.subject if mcp.context.auth else None,
    }
```

`mcp.context.meta` contains the request `_meta` object, including fields such as `progressToken`.

## Async tools

Async tools, resources, prompts, and JSON-RPC methods are supported. HTTP and stdio transports stay synchronous by default; async stdio concurrency is opt-in with `await mcp.stdio_async()`.

```python
import asyncio

@mcp.tool
async def slow_lookup(key: str) -> str:
    await asyncio.sleep(1)
    return key
```

## OAuth resource server

zeromcp can act as an MCP OAuth resource server. Token validation is provided by your application:

```python
from zeromcp import McpAuthInfo

@mcp.oauth(
    resource="https://mcp.example.com/mcp",
    authorization_servers=["https://auth.example.com"],
    scopes_supported=["mcp"],
    required_scopes=["mcp"],
)
def verify_token(token: str, resource: str) -> McpAuthInfo | None:
    if token == "expected":
        return McpAuthInfo(
            subject="user-123",
            scopes=frozenset({"mcp"}),
            claims={"sub": "user-123"},
        )
    return None
```

When OAuth is configured, HTTP MCP requests require `Authorization: Bearer <token>`. zeromcp exposes OAuth Protected Resource Metadata at `/.well-known/oauth-protected-resource`.

The example server includes a static-token OAuth verifier for local testing:

```bash
uv run examples/mcp_example.py --transport http://127.0.0.1:5001 --oauth --oauth-token dev-token --oauth-resource http://127.0.0.1:5001/mcp
```

## CORS

By default, zeromcp allows CORS requests from localhost origins (`localhost`, `127.0.0.1`, `::1`) on **any port**. This allows tools like the MCP Inspector or local AI tools to communicate with your MCP server.

```python
from zeromcp import McpServer

mcp = McpServer("my-server")

# Default: allow localhost on any port
mcp.cors_allowed_origins = mcp.cors_localhost

# Allow all origins (use with caution)
mcp.cors_allowed_origins = "*"

# Allow specific origins
mcp.cors_allowed_origins = [
    "http://localhost:3000",
    "https://myapp.example.com",
]

# Disable CORS (blocks all browser cross-origin requests)
mcp.cors_allowed_origins = None

# Custom logic
mcp.cors_allowed_origins = lambda origin: origin.endswith(".example.com")
```

Note: CORS only affects browser-based requests. Non-browser clients like `curl` or MCP desktop apps are unaffected by this setting.

## Supported clients

The following clients have been tested:

- [Claude Code](https://code.claude.com/docs/en/mcp#installing-mcp-servers)
- [Claude Desktop](https://modelcontextprotocol.io/docs/develop/connect-local-servers#installing-the-filesystem-server) (_stdio only_)
- [Visual Studio Code](https://code.visualstudio.com/docs/copilot/customization/mcp-servers)
- [Roo Code](https://docs.roocode.com/features/mcp/using-mcp-in-roo) / [Cline](https://docs.cline.bot/mcp/configuring-mcp-servers) / [Kilo Code](https://kilocode.ai/docs/features/mcp/using-mcp-in-kilo-code)
- [LM Studio](https://lmstudio.ai/docs/app/mcp)
- [Jan](https://www.jan.ai/docs/desktop/mcp#configure-and-use-mcps-within-jan)
- [Gemini CLI](https://geminicli.com/docs/tools/mcp-server/#how-to-set-up-your-mcp-server)
- [Cursor](https://cursor.com/docs/context/mcp)
- [Windsurf](https://docs.windsurf.com/windsurf/cascade/mcp)
- [Zed](https://zed.dev/docs/ai/mcp) (_stdio only_)
- [Warp](https://docs.warp.dev/knowledge-and-collaboration/mcp#adding-an-mcp-server)

_Note_: generally the `/mcp` endpoint is preferred, but not all clients support it correctly.

<a name="ai-usage"></a>
<sup>1</sup>README and some of the tests written by Claude
