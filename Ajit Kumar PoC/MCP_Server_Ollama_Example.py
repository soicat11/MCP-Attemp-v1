# mcp_server.py
from fastmcp import FastMCP

# Create the MCP server instance
mcp = FastMCP("My First MCP Server")

# Define Tool 1: Add two numbers
@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers together"""
    return a + b

# Define Tool 2: Greet someone
@mcp.tool()
def greet(name: str) -> str:
    """Greet someone by name and use the words Subho Noboborsho at the end"""
    return f"Nomoshkar, {name}! Welcome! Subho Noboborsho!!"

# Define Tool 3: Multiply numbers
@mcp.tool()
def multiply(a: float, b: float) -> float:
    """Multiply two numbers"""
    return a * b

# Define Tool 4: Get current time
@mcp.tool()
def get_time() -> str:
    """Get the current time"""
    from datetime import datetime
    return datetime.now().strftime("%I:%M %p")

if __name__ == '__main__':
    # Start the server
    mcp.run(transport='sse', port=8080)