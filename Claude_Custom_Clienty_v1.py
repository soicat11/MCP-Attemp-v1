"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          PowerBI MCP Client — Claude-Powered AI Assistant                   ║
║                                                                              ║
║  A fully-featured Python client that:                                        ║
║    • Spawns the PowerBI Modeling MCP server as a subprocess (stdio)          ║
║    • Discovers all available MCP tools dynamically                           ║
║    • Uses Claude (Anthropic) as the LLM brain                                ║
║    • Implements an autonomous tool-calling loop (like GitHub Copilot/Claude) ║
║    • Gives back natural language answers grounded in live PowerBI data       ║
╚══════════════════════════════════════════════════════════════════════════════╝

PREREQUISITES
─────────────
  pip install anthropic

HOW TO RUN
──────────
  1. Set your Anthropic API key:
       Windows : set ANTHROPIC_API_KEY=sk-ant-...
       Linux   : export ANTHROPIC_API_KEY=sk-ant-...

  2. Set the path to the PowerBI MCP executable:
       Windows : set POWERBI_MCP_EXE=C:\\MCPServers\\...\\powerbi-modeling-mcp.exe
       Linux   : export POWERBI_MCP_EXE=/path/to/powerbi-modeling-mcp

  3. Run:
       python powerbi_mcp_client.py

ARCHITECTURE OVERVIEW
──────────────────────

  ┌─────────────────────────────────────────────────────────┐
  │                      Python Process                     │
  │                                                         │
  │   ┌──────────────┐    JSON-RPC    ┌──────────────────┐  │
  │   │  MCP Client  │ ◄───stdin───► │  PowerBI MCP     │  │
  │   │  (this file) │ ───stdout──►  │  Server (.exe)   │  │
  │   └──────┬───────┘               └──────────────────┘  │
  │          │  HTTPS                                       │
  │   ┌──────▼───────┐                                      │
  │   │  Anthropic   │  (Claude processes user intent and   │
  │   │  Claude API  │   decides which MCP tools to call)   │
  │   └──────────────┘                                      │
  └─────────────────────────────────────────────────────────┘

FLOW FOR EACH USER MESSAGE
───────────────────────────
  User prompt
      │
      ▼
  Build message with tool definitions injected
      │
      ▼
  Send to Claude API  ──► Claude thinks
      │                        │
      │          ◄─────────────┘
      ▼
  Is response a tool_use block?
    YES → Call MCP server with that tool + args
         → Feed result back to Claude
         → Loop until Claude produces a plain text answer
    NO  → Print final answer to user
"""

# ─────────────────────────────────────────────────────────────────────────────
# Standard library imports
# ─────────────────────────────────────────────────────────────────────────────
import os           # Environment variables, path manipulation
import sys          # sys.exit on fatal errors
import json         # JSON serialisation / deserialisation
import subprocess   # Spawning the MCP server .exe as a child process
import threading    # Background thread to drain the server's stderr (avoids deadlocks)
import time         # Small sleeps while waiting for the server to start
import textwrap     # Pretty-printing long text in the terminal

# ─────────────────────────────────────────────────────────────────────────────
# Third-party imports
# ─────────────────────────────────────────────────────────────────────────────
try:
    import anthropic   # Official Anthropic SDK  (pip install anthropic)
except ImportError:
    print("ERROR: 'anthropic' package not found. Run:  pip install anthropic")
    sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONFIGURATION
# ═════════════════════════════════════════════════════════════════════════════

# Path to the PowerBI Modeling MCP server executable.
# Override this via the POWERBI_MCP_EXE environment variable.
POWERBI_MCP_EXE: str = os.environ.get(
    "POWERBI_MCP_EXE",
    r"C:\MCPServers\PowerBIModelingMCP\extension\server\powerbi-modeling-mcp.exe",
)

# Optional extra CLI flags passed to the server.
#   --start          → required; tells the server to begin the MCP session
#   --readonly       → (optional) prevents accidental writes to the model
#   --skipconfirmation → (optional) suppresses interactive yes/no prompts
MCP_SERVER_ARGS: list[str] = ["--start"]

# Claude model to use as the LLM brain.
# Always use the latest Sonnet for the best balance of speed and capability.
CLAUDE_MODEL: str = "claude-sonnet-4-20250514"

# Maximum tokens Claude may produce in a single response.
MAX_TOKENS: int = 4096

# Visual width for the terminal output ruler lines.
TERMINAL_WIDTH: int = 72


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — JSON-RPC HELPERS
# ═════════════════════════════════════════════════════════════════════════════
# The MCP protocol is built on top of JSON-RPC 2.0.
# Every message is a UTF-8 JSON object terminated by a newline (\n).

class JsonRpcError(Exception):
    """Raised when the MCP server returns a JSON-RPC error object."""
    pass


def _make_request(method: str, params: dict | None = None, req_id: int = 1) -> str:
    """
    Serialise a JSON-RPC 2.0 *request* message to a newline-terminated string.

    Parameters
    ----------
    method  : The RPC method name, e.g. "initialize", "tools/list", "tools/call"
    params  : Optional dictionary of parameters for the method
    req_id  : Integer identifier — the server echoes this back in its response
              so we can correlate replies to requests (important for async flows)
    """
    payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params:
        payload["params"] = params
    # json.dumps with ensure_ascii=False preserves Unicode in DAX/Power BI names
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _make_notification(method: str, params: dict | None = None) -> str:
    """
    Serialise a JSON-RPC 2.0 *notification* (a request with NO id field).
    Notifications do not expect a response from the server.
    Used for: "notifications/initialized" handshake message.
    """
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if params:
        payload["params"] = params
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _parse_response(raw_line: str) -> dict:
    """
    Parse one newline-terminated JSON-RPC response line from the server.
    Raises JsonRpcError if the server returned an error object.
    Raises ValueError if the line is not valid JSON.
    """
    data: dict = json.loads(raw_line)
    if "error" in data:
        err = data["error"]
        raise JsonRpcError(f"MCP error {err.get('code')}: {err.get('message')}")
    return data


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — MCP SERVER SUBPROCESS MANAGER
# ═════════════════════════════════════════════════════════════════════════════

class MCPServerProcess:
    """
    Manages the lifecycle of the PowerBI MCP server as a child process.

    The MCP spec (stdio transport) works like this:
      • The client writes JSON-RPC messages to the server's STDIN.
      • The server writes JSON-RPC responses to its STDOUT.
      • The server may write diagnostic/log lines to its STDERR
        (these are NOT JSON-RPC and must be drained separately).

    We spawn the .exe with subprocess.Popen, keeping stdin/stdout as PIPE
    so we can write/read programmatically.  A background daemon thread
    continuously reads STDERR to prevent the OS pipe buffer from filling up
    (which would deadlock the server).
    """

    def __init__(self, exe_path: str, extra_args: list[str]) -> None:
        self._exe_path   = exe_path
        self._extra_args = extra_args
        self._process: subprocess.Popen | None = None
        self._req_counter: int = 0   # Auto-incrementing JSON-RPC request id

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the server subprocess and perform the MCP initialisation handshake."""
        if not os.path.isfile(self._exe_path):
            print(f"\n[ERROR] MCP executable not found: {self._exe_path}")
            print("  Set the POWERBI_MCP_EXE environment variable to the correct path.")
            sys.exit(1)

        print(f"  Starting MCP server: {self._exe_path}")
        self._process = subprocess.Popen(
            [self._exe_path] + self._extra_args,
            stdin=subprocess.PIPE,    # We write JSON-RPC here
            stdout=subprocess.PIPE,   # We read JSON-RPC from here
            stderr=subprocess.PIPE,   # Drain in background thread
            text=True,                # Strings, not bytes
            encoding="utf-8",
            bufsize=1,                # Line-buffered — each \n flushes the buffer
        )

        # Drain stderr in a daemon thread so it never blocks the main thread.
        self._start_stderr_drain()

        # Give the server a moment to initialise its internal state.
        time.sleep(0.5)

        # Perform the mandatory MCP handshake (initialize → initialized).
        self._handshake()

    def stop(self) -> None:
        """Gracefully terminate the server subprocess."""
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _start_stderr_drain(self) -> None:
        """
        Launch a daemon thread that continuously reads from the server's
        stderr and (optionally) logs it.  Without this, a large amount of
        stderr output would fill the OS pipe buffer and hang the server.
        """
        def drain() -> None:
            for line in self._process.stderr:
                # Uncomment the next line to see raw server diagnostic output:
                # print(f"  [server stderr] {line.rstrip()}", flush=True)
                pass

        t = threading.Thread(target=drain, daemon=True, name="stderr-drain")
        t.start()

    def _handshake(self) -> None:
        """
        Execute the mandatory two-step MCP initialisation handshake.

        Step 1 — Client sends "initialize" with its capabilities.
                  Server replies with its own capabilities and the list of
                  protocol versions it supports.
        Step 2 — Client sends "notifications/initialized" (a notification,
                  no response expected) to signal it is ready.

        After this, the session is fully established and tools/list can be called.
        """
        # ── Step 1: initialize ───────────────────────────────────────────
        init_params = {
            "protocolVersion": "2024-11-05",   # MCP spec version we support
            "capabilities": {
                "tools": {}                     # We can call tools
            },
            "clientInfo": {
                "name":    "powerbi-python-client",
                "version": "1.0.0",
            },
        }
        response = self.call("initialize", init_params)
        server_info = response.get("result", {}).get("serverInfo", {})
        print(f"  MCP handshake OK  |  server: {server_info.get('name', 'unknown')} "
              f"v{server_info.get('version', '?')}")

        # ── Step 2: notifications/initialized ───────────────────────────
        # This is a fire-and-forget notification; the server does NOT reply.
        notif = _make_notification("notifications/initialized")
        self._process.stdin.write(notif)
        self._process.stdin.flush()

    # ── Public API ───────────────────────────────────────────────────────────

    def call(self, method: str, params: dict | None = None) -> dict:
        """
        Send a JSON-RPC request and block until the server's response arrives.

        This is a synchronous call — suitable because our conversation loop
        is single-threaded and we always wait for one reply before the next.

        Returns the full parsed response dict (including "result" key).
        """
        self._req_counter += 1
        message = _make_request(method, params, req_id=self._req_counter)

        # Write to the server's stdin
        self._process.stdin.write(message)
        self._process.stdin.flush()

        # Read exactly one response line from stdout.
        # readline() blocks until a '\n' appears — the server guarantees one
        # JSON-RPC response per request, so this is safe.
        raw_line = self._process.stdout.readline()
        if not raw_line:
            raise RuntimeError("MCP server closed its stdout unexpectedly.")

        return _parse_response(raw_line)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — MCP TOOL CATALOGUE
# ═════════════════════════════════════════════════════════════════════════════

def fetch_tools(server: MCPServerProcess) -> list[dict]:
    """
    Ask the MCP server to enumerate all of its tools (tools/list).

    The server returns a list of tool definitions, each containing:
      • name        — unique snake_case identifier, e.g. "connect_database"
      • description — human-readable purpose of the tool
      • inputSchema — JSON Schema describing the tool's parameters

    We convert these into Anthropic's "tool" format so Claude can
    understand and call them.

    MCP format (from server):
        {
          "name": "run_dax_query",
          "description": "Executes a DAX query ...",
          "inputSchema": {
            "type": "object",
            "properties": { "query": {"type": "string"} },
            "required": ["query"]
          }
        }

    Anthropic format (for Claude):
        {
          "name": "run_dax_query",
          "description": "Executes a DAX query ...",
          "input_schema": {        ← note the renamed key
            "type": "object",
            "properties": { "query": {"type": "string"} },
            "required": ["query"]
          }
        }
    """
    response  = server.call("tools/list")
    raw_tools = response.get("result", {}).get("tools", [])

    anthropic_tools: list[dict] = []
    for tool in raw_tools:
        anthropic_tools.append({
            "name":         tool["name"],
            "description":  tool.get("description", ""),
            # MCP uses "inputSchema"; Anthropic SDK uses "input_schema"
            "input_schema": tool.get("inputSchema", {"type": "object", "properties": {}}),
        })

    return anthropic_tools


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — TOOL EXECUTION BRIDGE
# ═════════════════════════════════════════════════════════════════════════════

def execute_tool(server: MCPServerProcess, tool_name: str, tool_input: dict) -> str:
    """
    Forward a tool call from Claude to the MCP server and return the result.

    Claude produces tool_use blocks like:
        { "type": "tool_use", "name": "connect_database", "input": {...} }

    We translate this into a JSON-RPC "tools/call" request:
        { "method": "tools/call", "params": { "name": "...", "arguments": {...} } }

    The server executes the tool (e.g., connects to a Power BI dataset,
    runs a DAX query, lists tables) and returns a "content" array.

    We concatenate all text blocks in the content array and hand the
    combined string back to Claude as a tool_result message.
    """
    print(f"\n  🔧  Calling MCP tool: {tool_name}")
    if tool_input:
        # Pretty-print the arguments with 4-space indent for readability
        pretty_args = json.dumps(tool_input, indent=4, ensure_ascii=False)
        for line in pretty_args.splitlines():
            print(f"       {line}")

    response = server.call(
        "tools/call",
        {"name": tool_name, "arguments": tool_input},
    )

    # Extract text content from the tool result.
    # MCP content blocks can be "text", "image", or "resource" type.
    # We only handle "text" here (sufficient for all PowerBI modeling tools).
    content_blocks = response.get("result", {}).get("content", [])
    result_parts: list[str] = []

    for block in content_blocks:
        if block.get("type") == "text":
            result_parts.append(block.get("text", ""))
        elif block.get("type") == "image":
            result_parts.append("[image result — not displayed in terminal]")
        else:
            # Fallback: serialise unknown block types as JSON
            result_parts.append(json.dumps(block))

    combined = "\n".join(result_parts).strip()
    print(f"\n  ✅  Tool result preview: {combined[:200]}{'…' if len(combined) > 200 else ''}")
    return combined


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — AI AGENT LOOP
# ═════════════════════════════════════════════════════════════════════════════

class PowerBIAgent:
    """
    The AI brain of the client.

    This class owns:
      • The conversation history (list of message dicts sent to Claude)
      • The list of MCP tools (injected into every API call so Claude can use them)
      • The agentic loop logic: call Claude → if tool_use, run tool → repeat

    The loop mirrors exactly what GitHub Copilot and Claude.ai do internally
    when they have access to tools.
    """

    def __init__(
        self,
        anthropic_client: anthropic.Anthropic,
        mcp_server: MCPServerProcess,
        tools: list[dict],
    ) -> None:
        self._client  = anthropic_client
        self._server  = mcp_server
        self._tools   = tools

        # Conversation history: a list of {"role": "user"|"assistant", "content": ...}
        # We keep the full history so Claude maintains context across turns.
        self._history: list[dict] = []

        # System prompt tells Claude its role and constraints.
        self._system_prompt: str = (
            "You are an expert Power BI AI assistant with access to a live "
            "Power BI Modeling MCP server. You can connect to semantic models, "
            "run DAX queries, list tables and measures, modify model metadata, "
            "and perform trace operations — all through the tools provided.\n\n"
            "Guidelines:\n"
            "• Always connect to the target database/model first before querying.\n"
            "• When a user asks a data question, write and run the appropriate "
            "  DAX query; do not guess the answer.\n"
            "• Explain what you are doing in plain English before and after each tool call.\n"
            "• If a tool returns an error, diagnose it and try an alternative approach.\n"
            "• Keep answers concise and actionable."
        )

    def chat(self, user_message: str) -> str:
        """
        Process one user turn through the full agentic loop.

        Algorithm
        ─────────
        1. Append the user's message to history.
        2. Send history + tools to Claude.
        3. If Claude responds with text only  → return that text.
        4. If Claude responds with tool_use   →
               a. Execute each requested tool on the MCP server.
               b. Append the assistant's message AND the tool results to history.
               c. Go to step 2 (Claude will synthesise a final answer).
        5. Repeat until a text-only response is received.

        This loop can run for many iterations on complex multi-step tasks —
        for example: connect → list tables → write DAX → run DAX → format result.
        """
        # Add the user's message to conversation history
        self._history.append({"role": "user", "content": user_message})

        max_iterations = 20   # Safety limit — prevents runaway loops
        iteration      = 0

        while iteration < max_iterations:
            iteration += 1

            # ── Call Claude ──────────────────────────────────────────────────
            response = self._client.messages.create(
                model      = CLAUDE_MODEL,
                max_tokens = MAX_TOKENS,
                system     = self._system_prompt,
                tools      = self._tools,   # ← The full MCP tool catalogue
                messages   = self._history,
            )

            # ── Inspect the stop reason ──────────────────────────────────────
            stop_reason: str = response.stop_reason  # "end_turn" or "tool_use"

            if stop_reason == "end_turn":
                # Claude finished — extract the final text response
                final_text = self._extract_text(response.content)

                # Record the assistant's reply in history for future turns
                self._history.append({
                    "role":    "assistant",
                    "content": response.content,
                })
                return final_text

            elif stop_reason == "tool_use":
                # Claude wants to call one or more tools.
                # We must:
                #   1. Record Claude's full response (including tool_use blocks) in history
                #   2. Execute each tool on the MCP server
                #   3. Append a "user" message containing the tool results
                #   4. Loop back to call Claude again

                # Step 1: Record assistant message
                self._history.append({
                    "role":    "assistant",
                    "content": response.content,
                })

                # Step 2 & 3: Execute tools and collect results
                tool_results: list[dict] = []
                for block in response.content:
                    if block.type == "tool_use":
                        result_text = execute_tool(
                            self._server,
                            tool_name  = block.name,
                            tool_input = block.input,
                        )
                        tool_results.append({
                            "type":        "tool_result",
                            "tool_use_id": block.id,   # Must echo the same id Claude sent
                            "content":     result_text,
                        })

                # Step 4: Feed results back to Claude as a "user" message.
                # This is the standard MCP/Anthropic tool_result pattern.
                self._history.append({
                    "role":    "user",
                    "content": tool_results,
                })
                # Loop continues — Claude will now synthesise an answer

            else:
                # Unexpected stop reason (e.g., "max_tokens" hit)
                return (f"[Stopped unexpectedly: stop_reason={stop_reason}. "
                        f"Try rephrasing your question or increasing MAX_TOKENS.]")

        return "[Agent loop limit reached. The task may require more iterations.]"

    @staticmethod
    def _extract_text(content_blocks) -> str:
        """
        Pull all TextBlock strings out of Claude's response content list
        and join them into a single string.
        """
        parts: list[str] = []
        for block in content_blocks:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts).strip()

    def reset_history(self) -> None:
        """Clear conversation history to start a fresh context."""
        self._history.clear()
        print("  Conversation history cleared.")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — TERMINAL UI HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def ruler(char: str = "─") -> None:
    """Print a horizontal line of `char` across the terminal width."""
    print(char * TERMINAL_WIDTH)


def print_welcome(tools: list[dict]) -> None:
    """Display startup banner and list discovered tools."""
    ruler("═")
    print("  PowerBI MCP Client  •  Claude AI  •  Python")
    ruler("═")
    print(f"\n  Claude model : {CLAUDE_MODEL}")
    print(f"  MCP server   : {POWERBI_MCP_EXE}")
    print(f"\n  Discovered {len(tools)} MCP tools:")
    for t in tools:
        # Wrap long descriptions so they fit in the terminal
        desc = t.get("description", "")[:80]
        print(f"    • {t['name']:<35} {desc}")
    print()
    ruler()
    print("  Type your question below. Special commands:")
    print("    /tools   — re-list all available MCP tools")
    print("    /reset   — clear conversation history")
    print("    /quit    — exit the client")
    ruler()
    print()


def print_response(text: str) -> None:
    """Pretty-print Claude's final answer with word-wrap."""
    ruler()
    print("\n  🤖  Assistant:\n")
    # Wrap lines at TERMINAL_WIDTH - 4 characters (leaving indent room)
    for paragraph in text.split("\n"):
        if paragraph.strip():
            wrapped = textwrap.fill(
                paragraph,
                width         = TERMINAL_WIDTH - 4,
                initial_indent    = "    ",
                subsequent_indent = "    ",
            )
            print(wrapped)
        else:
            print()
    print()
    ruler()
    print()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8 — MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def main() -> None:
    """
    Application entry point.

    Start-up sequence
    ─────────────────
    1. Validate the Anthropic API key is set.
    2. Spawn the PowerBI MCP server subprocess.
    3. Perform MCP handshake + tool discovery.
    4. Enter the interactive REPL (Read-Eval-Print Loop).
    """

    # ── 1. Validate API key ─────────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("\n[ERROR] ANTHROPIC_API_KEY environment variable is not set.")
        print("  Get your key at: https://console.anthropic.com/")
        sys.exit(1)

    # ── 2. Start the MCP server ─────────────────────────────────────────────
    print("\nStarting PowerBI MCP client …\n")
    server = MCPServerProcess(POWERBI_MCP_EXE, MCP_SERVER_ARGS)
    server.start()

    # ── 3. Discover tools ───────────────────────────────────────────────────
    print("  Fetching tool catalogue from MCP server …")
    tools = fetch_tools(server)

    # ── 4. Build the Anthropic client and agent ─────────────────────────────
    anthropic_client = anthropic.Anthropic(api_key=api_key)
    agent = PowerBIAgent(anthropic_client, server, tools)

    print_welcome(tools)

    # ── 5. REPL ─────────────────────────────────────────────────────────────
    try:
        while True:
            try:
                user_input = input("  You: ").strip()
            except (EOFError, KeyboardInterrupt):
                # Ctrl+D or Ctrl+C → clean exit
                print("\n\n  Goodbye!")
                break

            if not user_input:
                continue

            # ── Special slash-commands ──────────────────────────────────────
            if user_input.lower() in ("/quit", "/exit", "/q"):
                print("\n  Goodbye!")
                break

            if user_input.lower() == "/tools":
                print(f"\n  {len(tools)} available MCP tools:")
                for t in tools:
                    print(f"    • {t['name']}")
                    desc = t.get("description", "")
                    if desc:
                        print(f"      {desc[:100]}")
                print()
                continue

            if user_input.lower() == "/reset":
                agent.reset_history()
                continue

            # ── Normal chat turn ────────────────────────────────────────────
            print(f"\n  Thinking …\n")
            try:
                answer = agent.chat(user_input)
                print_response(answer)
            except JsonRpcError as e:
                print(f"\n  [MCP Error] {e}\n")
            except anthropic.APIError as e:
                print(f"\n  [Anthropic API Error] {e}\n")

    finally:
        # Always stop the server subprocess on exit
        server.stop()
        print("  MCP server stopped. Bye!\n")


# ─────────────────────────────────────────────────────────────────────────────
# Script entry point guard
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
