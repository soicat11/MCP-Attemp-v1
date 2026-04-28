import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# async def main():
#  server_params = {
#  "command": r"C:\Users\Saikat\Documents\Python
# Projects\MCP_Training_Project_1\Power_BI_MCP\extension\server\powerbi-modeling-mcp.exe",
#  "args": ["--start"],
#  "env": {}
#  }

async def main():
    server_params = StdioServerParameters(
    command=r"C:\Users\Saikat\Documents\Python Projects\MCP_Training_Project_1\Power_BI_MCP\extension\server\powerbi-modeling-mcp.exe",
    args=["--start"],
    env={}
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:

            # Initialize MCP session
            await session.initialize()

            # List tools
            tools = await session.list_tools()
            print(tools)

            # print("\n=== AVAILABLE TOOLS ===\n")
            # for tool in tools:
                
                # print(tool[1])

                # with open("Run 4.txt", "a") as file:
                #     file.write(f"{tool}\n")
    

                # if isinstance(tool, tuple):
                #     for item in tool:
                #         if hasattr(item, "name") and hasattr(item, "description"):
                #             tool_obj = item
                #             break
                #         else:
                #             tool_obj = tool

                #             if tool_obj is None:
                #                 print("Skipping unknown tool format:", tool)
                #                 continue

                # print(f"Name: {tool_obj.name}")
                # print(f"Description: {tool_obj.description}")
                # print("-" * 40)
        



if __name__ == "__main__":
    asyncio.run(main())
