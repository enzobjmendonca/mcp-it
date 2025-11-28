from fastapi import FastAPI, APIRouter
from mcpit import MCPIt
import uvicorn
from pydantic import BaseModel

app = FastAPI()
router = APIRouter()
mcp_it = MCPIt("Proxy Tools", json_response=True)

# 1. Manual Proxy Example
# This defines a tool 'multiply' that forwards to http://localhost:8800/multiply
# The logic is handled automatically by mcp-it
@mcp_it.proxy(url="http://localhost:8800/multiply", method="POST", param_structure={"a": "query", "b": "query"})
def multiply(a: float, b: float):
    """Multiply two numbers"""
    pass

# 2. OpenAPI Binding Example
# We dynamically register tools from the Petstore OpenAPI spec.
print("Fetching OpenAPI spec...")
mcp_it.bind_openapi(
    openapi_url="http://localhost:8800/openapi.json",
    include_paths=["/hello", "/subtract_with_auth", "/next_fibonacci"]
)

# Build and mount
mcp_it.build(router, transport='streamable-http', mount_path="/mcp")
app.include_router(router)

if __name__ == "__main__":
    print("Starting Proxy Server...")
    print("MCP Endpoint: http://localhost:8802/mcp")
    uvicorn.run(app, host="0.0.0.0", port=8802)

