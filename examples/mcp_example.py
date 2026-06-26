"""Example MCP server with test tools"""

import time
import argparse
from urllib.parse import urlparse
from typing import Annotated, Optional, TypedDict, NotRequired
from zeromcp import McpToolError, McpServer, McpAuthInfo

mcp = McpServer("example")


@mcp.tool
def divide(
    numerator: Annotated[float, "Numerator"],
    denominator: Annotated[float, "Denominator"],
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
    age: Annotated[Optional[int], "Age of person"] = None,
) -> GreetingResponse:
    """Generate a greeting message"""
    if age is not None:
        return {
            "message": f"Hello, {name}! You are {age} years old.",
            "name": name,
            "age": age,
        }
    return {"message": f"Hello, {name}!", "name": name}


class SystemInfo(TypedDict):
    platform: Annotated[str, "Operating system platform"]
    python_version: Annotated[str, "Python version"]
    machine: Annotated[str, "Machine architecture"]
    timestamp: Annotated[float, "Current timestamp"]


@mcp.tool(read_only=True, destructive=False, idempotent=True, open_world=False)
def get_system_info() -> SystemInfo:
    """Get system information"""
    import platform

    return {
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "machine": platform.machine(),
        "timestamp": time.time(),
    }


@mcp.tool(read_only=True, destructive=False, idempotent=True, open_world=False)
def echo(text: Annotated[str, "Text to echo verbatim"]) -> str:
    """Return text verbatim"""
    return text


@mcp.tool(read_only=True, destructive=False, idempotent=False, open_world=False)
def slow_count(limit: Annotated[int, "How high to count"] = 10) -> str:
    """Long-running tool that supports cancellation"""
    for _ in range(limit):
        mcp.check_cancelled()
        time.sleep(1)
    return f"Counted to {limit}"


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
    | Annotated[str, "Single structure name"],
) -> list[StructInfo]:
    """Retrieve structure information by names"""
    return [
        StructInfo(
            {
                "name": name,
                "size": 128,  # Dummy size
                "fields": ["field1", "field2", "field3"],  # Dummy fields
            }
        )
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
    name: Annotated[str, "Name to greet from resource"],
) -> GreetingResponse:
    """Resource providing greeting message"""
    return greet(name)


@mcp.resource("example://error")
def error_resource() -> None:
    """Resource that always fails (for testing error handling)"""
    raise McpToolError("This is a resource error for testing purposes.")


@mcp.prompt
def code_review(
    code: Annotated[str, "Code to review"],
    language: Annotated[str, "Programming language"] = "python",
) -> str:
    """Review code for bugs and improvements"""
    return f"Please review this {language} code for bugs and improvements:\n\n```{language}\n{code}\n```"


@mcp.prompt
def summarize(
    text: Annotated[str, "Text to summarize"],
    max_sentences: Annotated[int, "Maximum sentences"] = 3,
) -> str:
    """Summarize text concisely"""
    return f"Summarize the following in {max_sentences} sentences or fewer:\n\n{text}"


def infer_oauth_resource(transport: str) -> str:
    url = urlparse(transport)
    if url.hostname is None or url.port is None:
        raise Exception(f"Invalid transport URL: {transport}")

    host = url.hostname
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"

    netloc = f"[{host}]:{url.port}" if ":" in host else f"{host}:{url.port}"
    return f"{url.scheme}://{netloc}/mcp"


def configure_oauth(
    *,
    resource: str,
    authorization_servers: list[str],
    scopes: list[str],
    expected_token: str,
    subject: str,
    resource_metadata_url: str | None,
) -> None:
    @mcp.oauth(
        resource=resource,
        authorization_servers=authorization_servers,
        scopes_supported=scopes,
        required_scopes=scopes,
        resource_metadata_url=resource_metadata_url,
    )
    def verify_token(token: str, token_resource: str) -> McpAuthInfo | None:
        if token != expected_token:
            return None
        return McpAuthInfo(
            subject=subject,
            scopes=frozenset(scopes),
            claims={"sub": subject, "aud": token_resource},
        )

    @mcp.tool(read_only=True, destructive=False, idempotent=True, open_world=False)
    def whoami() -> dict[str, object]:
        """Return OAuth subject and token claims"""
        auth = mcp.context.auth
        if auth is None:
            raise McpToolError("No OAuth context")
        return {
            "subject": auth.subject,
            "scopes": sorted(auth.scopes),
            "claims": dict(auth.claims),
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP Example Server")
    parser.add_argument(
        "--transport",
        help="Transport (stdio or http://host:port)",
        default="http://127.0.0.1:5001",
    )
    parser.add_argument(
        "--cors-origin",
        action="append",
        dest="cors_origins",
        help="Allowed browser CORS origin. Repeat for multiple origins, or use '*' for local testing.",
    )
    parser.add_argument(
        "--oauth",
        action="store_true",
        help="Require OAuth bearer tokens for HTTP MCP requests.",
    )
    parser.add_argument(
        "--oauth-token",
        default="dev-token",
        help="Bearer token accepted by the example OAuth verifier.",
    )
    parser.add_argument(
        "--oauth-subject",
        default="example-user",
        help="Subject returned for the accepted example OAuth token.",
    )
    parser.add_argument(
        "--oauth-resource",
        help="Protected resource identifier. Defaults to the inferred public /mcp URL.",
    )
    parser.add_argument(
        "--oauth-authorization-server",
        action="append",
        dest="oauth_authorization_servers",
        help="Authorization server URL advertised in OAuth metadata. Repeat for multiple servers.",
    )
    parser.add_argument(
        "--oauth-scope",
        action="append",
        dest="oauth_scopes",
        help="Required and supported OAuth scope. Repeat for multiple scopes.",
    )
    parser.add_argument(
        "--oauth-resource-metadata-url",
        help="External OAuth Protected Resource Metadata URL to advertise in WWW-Authenticate.",
    )
    args = parser.parse_args()
    if args.transport == "stdio":
        if args.oauth:
            raise Exception("--oauth requires an HTTP transport")
        mcp.stdio()
    else:
        url = urlparse(args.transport)
        if url.hostname is None or url.port is None:
            raise Exception(f"Invalid transport URL: {args.transport}")

        if args.cors_origins:
            mcp.cors_allowed_origins = "*" if "*" in args.cors_origins else args.cors_origins

        oauth_resource = None
        oauth_authorization_servers = None
        oauth_scopes = None
        if args.oauth:
            oauth_resource = args.oauth_resource or infer_oauth_resource(args.transport)
            oauth_authorization_servers = args.oauth_authorization_servers or ["https://auth.example.com"]
            oauth_scopes = args.oauth_scopes or ["mcp"]
            configure_oauth(
                resource=oauth_resource,
                authorization_servers=oauth_authorization_servers,
                scopes=oauth_scopes,
                expected_token=args.oauth_token,
                subject=args.oauth_subject,
                resource_metadata_url=args.oauth_resource_metadata_url,
            )

        print("Starting MCP Example Server...")
        if args.cors_origins:
            print(f"CORS origins: {mcp.cors_allowed_origins}")
        if args.oauth:
            print("OAuth enabled:")
            print(f"  resource: {oauth_resource}")
            print(f"  authorization servers: {oauth_authorization_servers}")
            print(f"  required scopes: {oauth_scopes}")
            print(f"  local test token: {args.oauth_token}")

        print("\nAvailable tools:")
        for name in mcp.tools.methods.keys():
            func = mcp.tools.methods[name]
            print(f"  - {name}: {func.__doc__}")

        print("\nAvailable resources:")
        for name in mcp.resources.methods.keys():
            func = mcp.resources.methods[name]
            print(f"  - {name}: {func.__doc__}")

        print("\nAvailable prompts:")
        for name in mcp.prompts.methods.keys():
            func = mcp.prompts.methods[name]
            print(f"  - {name}: {func.__doc__}")
        print()

        mcp.serve(url.hostname, url.port)

        try:
            input("\nServer is running, press Enter or Ctrl+C to stop...")
        except (KeyboardInterrupt, EOFError):
            print("\n\nStopping server...")
            mcp.stop()
