# mcp-it

**Transform any FastAPI server into an MCP (Model Context Protocol) server.**

mcp-it allows you to seamlessly convert your FastAPI routes into MCP tools. Decorate your FastAPI endpoints with `@mcp_it.mcp()` and your API becomes an MCP server that can be used with MCP-compatible clients.

## Features

- **Minimal Code Changes**: Convert existing FastAPI routes to MCP tools with a simple decorator
- **Automatic Parameter Detection**: Automatically detects path, query, and body parameters from your FastAPI routes - no boilerplate reimplementation
- **Header Forwarding**: Maps and preserves headers from MCP requests to your FastAPI endpoints
- **Dependency Injection**: Full support for FastAPI's dependency injection system
- **Pydantic Models**: Full integration with Pydantic models for request/response validation
- **Multiple Transports**: Support for both sse and streamable-http transports
- **Type Safety**: Maintains type hints and annotations for proper MCP tool introspection
- **Auto Docs**: Automatically documents the MCP tools with your functions docstrings

## Installation

```bash
pip install mcp-it
```

## Quick Start

```python
from fastapi import FastAPI, APIRouter
from mcpit import MCPIt

# Create your FastAPI app
app = FastAPI()

# Initialize MCPIt
mcp_it = MCPIt("My API Tools", json_response=True)

# Decorate your routes with @mcp_it.mcp()
@app.post("/multiply")
@mcp_it.mcp(mode='tool', description="Multiply two numbers")
def multiply(a: float, b: float):
    """Multiplies two numbers together."""
    return {"result": a * b}

# Build and mount the MCP server
mcp_it.build(app.router, transport='streamable-http', mount_path="/mcp")

# Your MCP server is now available at /mcp
```

## Usage

### Basic Example

```python
from fastapi import FastAPI, APIRouter
from mcpit import MCPIt
from pydantic import BaseModel, Field

app = FastAPI()
router = APIRouter()
mcp_it = MCPIt("Calculator Tools", json_response=True)

class CalculationInput(BaseModel):
    a: float = Field(..., description="The first number")
    b: float = Field(..., description="The second number")

@router.post("/add")
@mcp_it.mcp(mode='tool', name="add", description="Add two numbers")
def add(input: CalculationInput):
    """Adds two numbers together."""
    return {"result": input.a + input.b}

mcp_it.build(router, transport='streamable-http', mount_path="/mcp")
app.include_router(router)
```

### With Authentication

`mcp-it` automatically forwards headers from MCP requests, so your authentication dependencies work seamlessly:

```python
from fastapi import Header, Depends
from typing import Optional

def fake_auth_dependency(authorization: Optional[str] = Header(None)):
    """Validate authorization header"""
    if authorization:
        return True
    return False

@router.get("/protected")
@mcp_it.mcp(mode='tool', description="Protected endpoint")
def protected_route(auth_result: bool = Depends(fake_auth_dependency)):
    """A protected route that requires authentication."""
    return {"message": "Access granted", "authenticated": auth_result}
```

### With Dependency Injection

Full support for FastAPI's dependency injection:

```python
class BasicService:
    def __init__(self):
        self.counter = 0
    
    def increment(self) -> int:
        self.counter += 1
        return self.counter

service = BasicService()
def get_service() -> BasicService:
    return service

from typing import Annotated
from fastapi import Depends

ServiceDep = Annotated[BasicService, Depends(get_service)]

@router.get("/increment")
@mcp_it.mcp(mode='tool', description="Increment counter")
def increment(service: ServiceDep):
    """Increment the counter using dependency injection."""
    return {"count": service.increment()}
```

### Path and Query Parameters

`mcp-it` automatically detects and handles path and query parameters:

```python
@router.get("/users/{user_id}/posts")
@mcp_it.mcp(mode='tool', description="Get user posts")
def get_user_posts(user_id: int, limit: int = 10, offset: int = 0):
    """Get posts for a specific user with pagination."""
    return {
        "user_id": user_id,
        "posts": [],
        "limit": limit,
        "offset": offset
    }
```

## API Reference

### `MCPIt`

The main class for creating MCP servers from FastAPI routes.

#### Constructor

```python
MCPIt(name: str, json_response: bool = True)
```

- **name**: Name of your MCP server
- **json_response**: Whether to return JSON responses (default: True)

#### Methods

##### `mcp(mode='tool', **kwargs)`

Decorator to register a FastAPI route as an MCP capability.

**Parameters:**
- **mode**: Type of MCP capability (`'tool'`, `'resource'`, or `'prompt'`)
- **name**: (optional) Custom name for the tool (defaults to function name)
- **description**: (optional) Tool description (defaults to function docstring)

**Example:**
```python
@mcp_it.mcp(mode='tool', name="custom_name", description="Custom description")
def my_function():
    pass
```

##### `build(router, transport='streamable-http', mount_path='/mcp')`

Builds the MCP server and mounts it to your FastAPI router.

**Parameters:**
- **router**: Your FastAPI `APIRouter` instance
- **transport**: Transport type (`'sse'` or `'streamable-http'`)
- **mount_path**: Path where the MCP server will be mounted (default: `/mcp`)

**Example:**
```python
mcp_it.build(router, transport='streamable-http', mount_path="/mcp")
```

## How It Works

1. **Decorator Registration**: When you use `@mcp_it.mcp()`, the function is registered in an internal registry
2. **Route Analysis**: During `build()`, `mcp-it` analyzes your FastAPI routes to detect:
   - Path parameters
   - Query parameters
   - Body parameters
   - Pydantic models
3. **Tool Creation**: MCP tools are created with proper signatures and type annotations
4. **Internal Proxy**: When an MCP tool is called, `mcp-it` makes an internal ASGI call to your FastAPI route
5. **Header Forwarding**: Headers from MCP requests are automatically forwarded to your endpoints

## Transport Modes

### Streamable HTTP (Recommended)

```python
mcp_it.build(router, transport='streamable-http', mount_path="/mcp")
```

- Uses HTTP with streaming support
- Better for production environments
- Supports all HTTP methods

### SSE (Server-Sent Events)

```python
mcp_it.build(router, transport='sse', mount_path="/mcp")
```

- Uses Server-Sent Events for streaming
- Good for real-time updates
- Simpler connection model

## Development

### Setup

```bash
# Clone the repository
git clone https://github.com/enzobjmendonca/mcp-it.git
cd mcp-it

# Install development dependencies
pip install -r requirements.txt

# Install the package in editable mode
pip install -e .
```

### Running Tests

```bash
pytest
```

## Examples

See the `example.py` file in the repository for a complete working example with:
- Multiple route types
- Pydantic models
- Authentication
- Dependency injection
- Path and query parameters

## Requirements

- Python 3.8+
- FastAPI
- FastMCP
- httpx
- Pydantic

## Contributing

Contributions are more than welcome! Please feel free to submit a Pull Request.

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Links

- **GitHub**: [https://github.com/enzobjmendonca/mcp-it](https://github.com/enzobjmendonca/mcp-it)
- **PyPI**: [https://pypi.org/project/mcp-it](https://pypi.org/project/mcp-it)
- **Issues**: [https://github.com/enzobjmendonca/mcp-it/issues](https://github.com/enzobjmendonca/mcp-it/issues)
