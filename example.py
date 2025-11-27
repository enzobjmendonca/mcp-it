from dataclasses import Field
import uvicorn
from fastapi import FastAPI, Depends, APIRouter
from typing import Annotated, Optional
from pydantic import BaseModel
from fastapi import Header
from src.mcpit import MCPIt
from pydantic import Field

# 1. Define your FastAPI application
app = FastAPI(title="MCP It Example", version="1.0.0")
router = APIRouter()
# 2. Initialize MCPIt
mcp_it = MCPIt("Math Tools", json_response=True)

# --- Application Logic ---

class CalculationResult(BaseModel):
    operation: str
    a: float
    b: float
    result: float

def fake_auth_dependency(authorization: Optional[str] = Header(None)):
    """Simulate an auth dependency that might be used in your app"""
    # In a real app, this would validate a token.
    # MCPIt forwards the headers, so this works!
    if authorization:
        print(f"Authorized request for user: {authorization}")
        return True
    return False

# Example using internal dependency injection
class BasicService():
    def __init__(self):
        self.a = 0
        self.b = 1

    def next(self) -> float:
        result = self.a + self.b
        self.a = self.b
        self.b = result
        return result

def get_basic_service() -> BasicService:
    return BasicService()

BasicServiceDep = Annotated[BasicService, Depends(get_basic_service)]

# 3. Create Routes and Annotate them with @mcp_it.mcp()

class CalculationInput(BaseModel):
    a: float = Field(..., description="The first number.")
    b: float = Field(..., description="The second number.")

@router.post("/add_complex")
#@mcp_it.mcp(mode='tool', name="add_complex", description="Add two numbers using object input")
def add_complex(input1: CalculationInput, input2: CalculationInput):
    """
    Adds two numbers provided in an input object.
    """
    return {"result": input1.a + input1.b + input2.a + input2.b}

@router.post("/add_simple")
#@mcp_it.mcp(mode='tool', name="add_simple", description="Add two numbers using a single object input")
def add_simple(input: CalculationInput):
    """
    Adds two numbers provided in a single input object.
    """
    return {"result": input.a + input.b}

@router.post("/multiply")
#@mcp_it.mcp(mode='tool')
def multiply(a: float, b: float):
    """
    Multiplies two numbers.
    Args:
        a: The first number.
        b: The second number.
    Returns:
        A dictionary containing the result and the user token.
    """
    # You can return Pydantic models, dicts, or strings
    return CalculationResult(
        operation="multiply",
        a=a,
        b=b,
        result=a * b
    )

@router.get("/hello")
#@mcp_it.mcp(mode='tool', description="Say hello")
def hello():
    """Returns a simple greeting."""
    return {"message": "Hello from MCP!"}

@router.get("/subtract_with_auth")
#@mcp_it.mcp(mode='tool', description="Subtract two numbers with authentication")
def subtract_with_auth(a: float, b: float, auth_result: bool = Depends(fake_auth_dependency)):
    """
    Subtracts two numbers with authentication.
    """
    return {"result": a - b, "auth_result": auth_result}

@router.get("/next_fibonacci")
@mcp_it.mcp(mode='tool', name="next_fibonacci", description="Get the next Fibonacci number")
def next_fibonacci(service: BasicServiceDep):
    """
    Returns the next Fibonacci number.
    """
    return {"result": service.next()}

# 4. Build and Mount the MCP Server
# This creates endpoints at:
#   GET  /mcp/sse      (SSE Connection)
#   POST /mcp/messages (JSON-RPC Messages)
mcp_it.build(router, 'streamable-http', mount_path="/mcp")

app.include_router(router)
if __name__ == "__main__":
    print("Starting Server...")
    print("MCP Endpoint: http://localhost:8000/mcp")
    uvicorn.run(app, host="0.0.0.0", port=8000)
