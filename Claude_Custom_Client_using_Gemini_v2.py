"""
╔══════════════════════════════════════════════════════════════════════════════╗
║       PowerBI MCP Client — Gemini AI Studio (Free Tier)                     ║
║                                                                              ║
║  A fully-featured Python client that:                                        ║
║    • Spawns the PowerBI Modeling MCP server as a subprocess (stdio)          ║
║    • Discovers all available MCP tools dynamically                           ║
║    • Uses Google Gemini (via AI Studio free tier) as the LLM brain           ║
║    • Implements an autonomous tool-calling loop (like GitHub Copilot)        ║
║    • Gives back natural language answers grounded in live PowerBI data       ║
╚══════════════════════════════════════════════════════════════════════════════╝

PREREQUISITES
─────────────
  pip install google-genai

  Note: This uses the NEW 'google-genai' package (not the old 'google-generativeai').
  If you have the old one, uninstall it first:
      pip uninstall google-generativeai -y
      pip install google-genai

HOW TO GET A FREE GEMINI API KEY
──────────────────────────────────
  1. Go to https://aistudio.google.com/app/apikey
  2. Sign in with your Google account
  3. Click "Create API key" → "Create API key in new project"
  4. Copy the key (starts with "AIza...")
  The free tier gives you 1,500 requests/day on Gemini 2.0 Flash — plenty for this client.

HOW TO RUN
──────────
  1. Set your Gemini API key:
       Windows : set GEMINI_API_KEY=AIza...
       Linux   : export GEMINI_API_KEY=AIza...

  2. Set the path to the PowerBI MCP executable:
       Windows : set POWERBI_MCP_EXE=C:\\MCPServers\\...\\powerbi-modeling-mcp.exe
       Linux   : export POWERBI_MCP_EXE=/path/to/powerbi-modeling-mcp

  3. Run:
       python powerbi_mcp_client_gemini.py

ARCHITECTURE OVERVIEW
──────────────────────

  ┌─────────────────────────────────────────────────────────────┐
  │                       Python Process                        │
  │                                                             │
  │  ┌──────────────┐    JSON-RPC     ┌──────────────────────┐  │
  │  │  MCP Client  │ ◄──stdin────►  │  PowerBI MCP         │  │
  │  │  (this file) │ ───stdout───►  │  Server (.exe)       │  │
  │  └──────┬───────┘                └──────────────────────┘  │
  │         │  HTTPS                                            │
  │  ┌──────▼───────┐                                           │
  │  │  Google      │  (Gemini decides which MCP tools to call  │
  │  │  Gemini API  │   and generates natural language answers) │
  │  └──────────────┘                                           │
  └─────────────────────────────────────────────────────────────┘

HOW THE GEMINI TOOL-CALLING LOOP WORKS
────────────────────────────────────────
  Unlike Anthropic, Gemini uses a CHAT SESSION object that maintains
  conversation history automatically. Each turn works like this:

  User prompt
      │
      ▼
  chat.send_message(prompt, tools=[...])
      │
      ▼
  Gemini responds — check response.candidates[0].content.parts
      │
      ├── part.function_call exists?
      │       YES → Execute tool on MCP server
      │            → Send result back via chat.send_message([Part(function_response=...)])
      │            → Loop back to check response again
      │
      └── part.text exists?
              YES → Print final answer ✅
"""

# ─────────────────────────────────────────────────────────────────────────────
# Standard library imports
# ─────────────────────────────────────────────────────────────────────────────
import os           # Environment variables
import sys          # sys.exit on fatal errors
import json         # JSON serialisation / deserialisation
import subprocess   # Spawning the MCP .exe as a child process
import threading    # Background thread to drain server stderr
import time         # Small sleep while server starts
import textwrap     # Word-wrapping terminal output

# ─────────────────────────────────────────────────────────────────────────────
# Third-party imports — Google Gemini SDK
# ─────────────────────────────────────────────────────────────────────────────
try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    print("ERROR: 'google-genai' package not found.")
    print("  Run:  pip install google-genai")
    sys.exit(1)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — CONFIGURATION
# ═════════════════════════════════════════════════════════════════════════════

# Path to the PowerBI Modeling MCP server executable.
# Override via the POWERBI_MCP_EXE environment variable.
POWERBI_MCP_EXE: str = os.environ.get(
    "POWERBI_MCP_EXE",
    r"C:\Users\Saikat\Documents\Python Projects\MCP_Training_Project_1\Power_BI_MCP\extension\server\powerbi-modeling-mcp.exe",
)

# CLI flags passed to the MCP server.
#   --start           → required; begins the MCP session
#   --readonly        → (optional) prevents accidental writes to the model
#   --skipconfirmation→ (optional) suppresses interactive prompts
MCP_SERVER_ARGS: list[str] = ["--start"]

# Gemini model to use.
# gemini-2.0-flash is FREE on AI Studio (1,500 req/day) and supports tool calling.
# Other free-tier options: gemini-1.5-flash, gemini-1.5-pro (lower rate limits)
GEMINI_MODEL: str = "gemini-2.0-flash"

# Maximum tokens Gemini may produce in a single response.
MAX_OUTPUT_TOKENS: int = 4096

# Visual width for terminal output rulers.
TERMINAL_WIDTH: int = 72


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — JSON-RPC HELPERS
# ═════════════════════════════════════════════════════════════════════════════
# The MCP protocol runs over JSON-RPC 2.0.
# Every message is a UTF-8 JSON object terminated by a newline (\n).
# This section has NOT changed from the Anthropic version — the MCP wire
# protocol is LLM-agnostic; only the AI layer (Section 6) changes.

class JsonRpcError(Exception):
    """Raised when the MCP server returns a JSON-RPC error object."""
    pass


def _make_request(method: str, params: dict | None = None, req_id: int = 1) -> str:
    """
    Serialise a JSON-RPC 2.0 *request* to a newline-terminated string.

    Parameters
    ----------
    method  : RPC method name e.g. "initialize", "tools/list", "tools/call"
    params  : Optional dict of parameters
    req_id  : Integer id — echoed back in the server's response so we can
              match replies to requests
    """
    payload: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params:
        payload["params"] = params
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _make_notification(method: str, params: dict | None = None) -> str:
    """
    Serialise a JSON-RPC 2.0 *notification* (no id → no reply expected).
    Used for the "notifications/initialized" handshake step.
    """
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if params:
        payload["params"] = params
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _parse_response(raw_line: str) -> dict:
    """
    Parse one newline-terminated JSON-RPC response line.
    Raises JsonRpcError if the server returned an error object.
    """
    data: dict = json.loads(raw_line)
    if "error" in data:
        err = data["error"]
        raise JsonRpcError(f"MCP error {err.get('code')}: {err.get('message')}")
    return data


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — MCP SERVER SUBPROCESS MANAGER
# ═════════════════════════════════════════════════════════════════════════════
# Manages the lifecycle of the PowerBI MCP server as a child process.
# Communication: we write JSON-RPC to its STDIN, read from its STDOUT.
# A background daemon thread drains STDERR to prevent pipe-buffer deadlocks.

class MCPServerProcess:
    """
    Manages the PowerBI MCP server subprocess.

    The MCP stdio transport works as follows:
      • Client  → writes JSON-RPC messages to server STDIN
      • Server  → writes JSON-RPC responses to its STDOUT
      • Server  → writes diagnostic/log lines to its STDERR
                   (these are NOT JSON-RPC; we drain them in a background thread)

    Why Popen and not subprocess.run()?
      subprocess.run() blocks until the child process exits.
      subprocess.Popen() launches the process and returns immediately,
      giving us live pipe handles to communicate with a long-running server.
    """

    def __init__(self, exe_path: str, extra_args: list[str]) -> None:
        self._exe_path    = exe_path
        self._extra_args  = extra_args
        self._process: subprocess.Popen | None = None
        self._req_counter: int = 0   # Auto-incrementing JSON-RPC request id

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the server subprocess and execute the MCP initialisation handshake."""
        if not os.path.isfile(self._exe_path):
            print(f"\n[ERROR] MCP executable not found: {self._exe_path}")
            print("  Set the POWERBI_MCP_EXE environment variable to the correct path.")
            sys.exit(1)

        print(f"  Starting MCP server: {self._exe_path}")
        self._process = subprocess.Popen(
            [self._exe_path] + self._extra_args,
            stdin=subprocess.PIPE,    # We write JSON-RPC here
            stdout=subprocess.PIPE,   # We read JSON-RPC from here
            stderr=subprocess.PIPE,   # Drained in background (prevents deadlock)
            text=True,                # Work with strings, not bytes
            encoding="utf-8",
            bufsize=1,                # Line-buffered: each \n flushes the buffer
        )

        # Drain stderr continuously in a background daemon thread.
        # Without this, the OS pipe buffer (≈64 KB) fills up when the server
        # emits lots of log lines, blocking the server and causing a deadlock.
        self._start_stderr_drain()

        # Brief pause to let the server initialise its internal state.
        time.sleep(0.5)

        # Perform the mandatory two-step MCP handshake.
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
        Launch a daemon thread that reads server stderr continuously and discards it.

        daemon=True  →  Python kills this thread automatically on exit,
                        so it never blocks the shutdown sequence.
        """
        def drain() -> None:
            for _ in self._process.stderr:
                # Uncomment to see raw server diagnostics:
                # print(f"  [server] {line.rstrip()}", flush=True)
                pass

        t = threading.Thread(target=drain, daemon=True, name="stderr-drain")
        t.start()

    def _handshake(self) -> None:
        """
        Execute the mandatory MCP initialisation handshake.

        Step 1 — Send "initialize": declare our capabilities and protocol version.
                  The server replies with its capabilities.
        Step 2 — Send "notifications/initialized": fire-and-forget signal that
                  the client is ready. No response is sent by the server.

        After this the session is fully established and tools/list may be called.
        """
        # Step 1: initialize
        init_params = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "powerbi-gemini-client", "version": "1.0.0"},
        }
        response = self.call("initialize", init_params)
        server_info = response.get("result", {}).get("serverInfo", {})
        print(f"  MCP handshake OK  |  server: {server_info.get('name', 'unknown')} "
              f"v{server_info.get('version', '?')}")

        # Step 2: notify server that we are ready
        notif = _make_notification("notifications/initialized")
        self._process.stdin.write(notif)
        self._process.stdin.flush()

    # ── Public API ───────────────────────────────────────────────────────────

    def call(self, method: str, params: dict | None = None) -> dict:
        """
        Send a JSON-RPC request and block until the server's response arrives.

        readline() on stdout blocks until a newline appears.
        The server sends exactly one JSON-RPC response per request, so this
        synchronous approach is both correct and simple.
        """
        self._req_counter += 1
        message = _make_request(method, params, req_id=self._req_counter)

        self._process.stdin.write(message)
        self._process.stdin.flush()

        raw_line = self._process.stdout.readline()
        if not raw_line:
            raise RuntimeError("MCP server closed its stdout unexpectedly.")

        return _parse_response(raw_line)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 — MCP TOOL CATALOGUE  →  GEMINI FORMAT
# ═════════════════════════════════════════════════════════════════════════════
# The MCP server describes its tools using JSON Schema.
# Gemini expects tools as a list of genai_types.FunctionDeclaration objects.
# This section converts between the two formats.

def fetch_tools(server: MCPServerProcess) -> list[genai_types.Tool]:
    """
    Ask the MCP server for its full tool list and convert to Gemini format.

    MCP tool format (from server):
    ─────────────────────────────
    {
      "name": "run_dax_query",
      "description": "Executes a DAX query against a connected model.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": {"type": "string", "description": "DAX query string"}
        },
        "required": ["query"]
      }
    }

    Gemini FunctionDeclaration format:
    ──────────────────────────────────
    FunctionDeclaration(
        name        = "run_dax_query",
        description = "Executes a DAX query against a connected model.",
        parameters  = Schema(
            type       = "OBJECT",
            properties = {"query": Schema(type="STRING", description="...")},
            required   = ["query"]
        )
    )

    Key differences from Anthropic:
      • MCP uses "inputSchema" → Gemini uses "parameters"
      • Gemini wraps all declarations in a single Tool() object
      • Gemini type strings are uppercase: "STRING" not "string"
    """
    response  = server.call("tools/list")
    raw_tools = response.get("result", {}).get("tools", [])

    function_declarations: list[genai_types.FunctionDeclaration] = []

    for tool in raw_tools:
        input_schema = tool.get("inputSchema", {})

        # Convert the JSON Schema properties dict into Gemini Schema objects.
        gemini_properties = _convert_properties(input_schema.get("properties", {}))

        parameters = genai_types.Schema(
            type       = genai_types.Type.OBJECT,
            properties = gemini_properties,
            required   = input_schema.get("required", []),
        )

        function_declarations.append(
            genai_types.FunctionDeclaration(
                name        = tool["name"],
                description = tool.get("description", ""),
                parameters  = parameters,
            )
        )

    # Gemini groups all function declarations into a single Tool wrapper object.
    return [genai_types.Tool(function_declarations=function_declarations)]


def _convert_properties(props: dict) -> dict[str, genai_types.Schema]:
    """
    Recursively convert a JSON Schema 'properties' dict into Gemini Schema objects.

    JSON Schema type → Gemini Type enum mapping:
      "string"  → Type.STRING
      "integer" → Type.INTEGER
      "number"  → Type.NUMBER
      "boolean" → Type.BOOLEAN
      "array"   → Type.ARRAY
      "object"  → Type.OBJECT
    """
    type_map: dict[str, genai_types.Type] = {
        "string":  genai_types.Type.STRING,
        "integer": genai_types.Type.INTEGER,
        "number":  genai_types.Type.NUMBER,
        "boolean": genai_types.Type.BOOLEAN,
        "array":   genai_types.Type.ARRAY,
        "object":  genai_types.Type.OBJECT,
    }

    result: dict[str, genai_types.Schema] = {}
    for prop_name, prop_schema in props.items():
        json_type = prop_schema.get("type", "string")

        if isinstance(json_type, list):          # e.g. ["string", "null"]
            non_null  = [t for t in json_type if t != "null"]
            json_type = non_null[0] if non_null else "string"

        gemini_type = type_map.get(json_type, genai_types.Type.STRING)

        # Recursively handle nested object schemas
        nested_props = {}
        if gemini_type == genai_types.Type.OBJECT and "properties" in prop_schema:
            nested_props = _convert_properties(prop_schema["properties"])

        result[prop_name] = genai_types.Schema(
            type        = gemini_type,
            description = prop_schema.get("description", ""),
            properties  = nested_props if nested_props else None,
        )

    return result


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 — TOOL EXECUTION BRIDGE
# ═════════════════════════════════════════════════════════════════════════════
# Translates a Gemini function_call into a MCP tools/call JSON-RPC request
# and returns the text result.

def execute_tool(server: MCPServerProcess, tool_name: str, tool_args: dict) -> str:
    """
    Forward a Gemini function_call to the MCP server and return the result text.

    Gemini produces function_call objects like:
        FunctionCall(name="connect_database", args={"database": "Sales"})

    We translate this to a JSON-RPC "tools/call" request:
        { "method": "tools/call", "params": { "name": "...", "arguments": {...} } }

    The result (a list of content blocks) is collapsed into a single string
    and returned to the agent loop, which feeds it back to Gemini.
    """
    # Convert Gemini's MapComposite args to a plain Python dict
    args_dict = dict(tool_args) if tool_args else {}

    print(f"\n  🔧  Calling MCP tool: {tool_name}")
    if args_dict:
        pretty = json.dumps(args_dict, indent=4, ensure_ascii=False)
        for line in pretty.splitlines():
            print(f"       {line}")

    response = server.call("tools/call", {"name": tool_name, "arguments": args_dict})

    content_blocks = response.get("result", {}).get("content", [])
    parts: list[str] = []
    for block in content_blocks:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        else:
            parts.append(json.dumps(block))

    combined = "\n".join(parts).strip()
    print(f"\n  ✅  Tool result preview: {combined[:200]}{'…' if len(combined) > 200 else ''}")
    return combined


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 — GEMINI AI AGENT LOOP
# ═════════════════════════════════════════════════════════════════════════════
# This is the biggest difference from the Anthropic version.
#
# Anthropic: we manually manage a `messages` list and pass it on every call.
# Gemini   : we use a `ChatSession` object that manages history automatically.
#            We just call chat.send_message() and Gemini handles the history.
#
# Gemini tool-call response structure:
#   response.candidates[0].content.parts  →  list of Part objects
#     Each Part is either:
#       • Part with .text         → final text answer
#       • Part with .function_call → Gemini wants to call a tool
#
# To return a tool result back to Gemini, we send a new message containing
# a Part built with genai_types.Part.from_function_response(...).

class PowerBIGeminiAgent:
    """
    The AI brain of the client — powered by Google Gemini.

    Uses a Gemini ChatSession for automatic conversation history management.
    Implements the full agentic loop:
      ask Gemini → if function_call → run MCP tool → return result → repeat
                → if text only     → print final answer ✅
    """

    def __init__(
        self,
        gemini_client: genai.Client,
        mcp_server: MCPServerProcess,
        tools: list[genai_types.Tool],
    ) -> None:
        self._client = gemini_client
        self._server = mcp_server
        self._tools  = tools

        # System instruction (equivalent to Anthropic's "system" parameter).
        # Tells Gemini its role and how to behave.
        self._system_instruction: str = (
            "You are an expert Power BI AI assistant with access to a live "
            "Power BI Modeling MCP server. You can connect to semantic models, "
            "run DAX queries, list tables and measures, modify model metadata, "
            "and perform trace operations — all through the tools provided.\n\n"
            "Guidelines:\n"
            "• Always connect to the target database/model first before querying.\n"
            "• When a user asks a data question, write and execute the appropriate "
            "  DAX query rather than guessing.\n"
            "• Explain what you are doing in plain English before and after each tool call.\n"
            "• If a tool returns an error, diagnose it and try an alternative approach.\n"
            "• Keep answers concise and actionable."
        )

        # Create a Gemini chat session.
        # The session object automatically appends each user/model turn to its
        # internal history, so we don't need to manage a messages list manually.
        self._chat = self._new_chat_session()

    def _new_chat_session(self):
        """
        Create a fresh Gemini chat session with our system instruction and tools.

        GenerateContentConfig holds all non-message settings:
          system_instruction → sets the model's persona and constraints
          tools              → list of Tool objects the model may call
          max_output_tokens  → cap on response length
          temperature        → 0.0 = deterministic/factual (best for tool use)
        """
        config = genai_types.GenerateContentConfig(
            system_instruction = self._system_instruction,
            tools              = self._tools,
            max_output_tokens  = MAX_OUTPUT_TOKENS,
            temperature        = 0.0,   # Low temperature = more reliable tool calls
        )
        return self._client.chats.create(
            model  = GEMINI_MODEL,
            config = config,
        )

    def chat(self, user_message: str) -> str:
        """
        Process one user turn through the full agentic tool-calling loop.

        Algorithm
        ──────────
        1. Send the user's message to Gemini via chat.send_message().
        2. Inspect the response parts:
             a. If any part has a .function_call  →
                  • Execute that tool on the MCP server
                  • Send the result back as a function_response Part
                  • Loop back to step 2 (Gemini may call more tools or answer)
             b. If any part has .text (and no function calls remain) →
                  • Return that text as the final answer
        3. Repeat until a text-only response is received.

        Note: Gemini may return MULTIPLE function_calls in one response
        (parallel tool use). We handle all of them before sending results back.
        """
        # Step 1: Send to Gemini
        response = self._chat.send_message(user_message)

        max_iterations = 20   # Safety limit — prevents infinite loops
        iteration      = 0

        while iteration < max_iterations:
            iteration += 1

            # ── Inspect response parts ────────────────────────────────────
            parts      = response.candidates[0].content.parts
            func_calls = [p for p in parts if p.function_call is not None]
            text_parts = [p for p in parts if p.text]

            if not func_calls:
                # No function calls → Gemini is done; collect and return text
                return "\n".join(p.text for p in text_parts if p.text).strip()

            # ── Execute all requested tool calls ──────────────────────────
            # Gemini may request multiple tools in one shot (parallel calls).
            # We execute them all and bundle the results into one reply message.
            function_response_parts: list[genai_types.Part] = []

            for part in func_calls:
                fc        = part.function_call
                tool_name = fc.name
                tool_args = dict(fc.args) if fc.args else {}

                result_text = execute_tool(self._server, tool_name, tool_args)

                # Build a Part containing the function response.
                # Gemini requires the response to be wrapped in {"result": ...}.
                function_response_parts.append(
                    genai_types.Part.from_function_response(
                        name     = tool_name,
                        response = {"result": result_text},
                    )
                )

            # ── Send all results back to Gemini in one message ────────────
            # The chat session adds this to its history automatically.
            response = self._chat.send_message(function_response_parts)
            # Loop — Gemini will now synthesise an answer or call more tools

        return "[Agent loop limit reached. The task may require more iterations.]"

    def reset_history(self) -> None:
        """Clear conversation history by creating a fresh chat session."""
        self._chat = self._new_chat_session()
        print("  Conversation history cleared.")


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 — TERMINAL UI HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def ruler(char: str = "─") -> None:
    print(char * TERMINAL_WIDTH)


def print_welcome(tools: list[genai_types.Tool]) -> None:
    """Display startup banner and list all discovered MCP tools."""
    ruler("═")
    print("  PowerBI MCP Client  •  Google Gemini AI Studio  •  Python")
    ruler("═")
    print(f"\n  Gemini model  : {GEMINI_MODEL}  (free tier — 1,500 req/day)")
    print(f"  MCP server    : {POWERBI_MCP_EXE}")

    all_decls = []
    for tool_obj in tools:
        all_decls.extend(tool_obj.function_declarations or [])

    print(f"\n  Discovered {len(all_decls)} MCP tools:")
    for decl in all_decls:
        desc = (decl.description or "")[:75]
        print(f"    • {decl.name:<35} {desc}")

    print()
    ruler()
    print("  Type your question below. Special commands:")
    print("    /tools   — re-list all available MCP tools")
    print("    /reset   — clear conversation history")
    print("    /quit    — exit the client")
    ruler()
    print()


def print_response(text: str) -> None:
    """Pretty-print Gemini's final answer with word-wrap."""
    ruler()
    print("\n  🤖  Gemini:\n")
    for paragraph in text.split("\n"):
        if paragraph.strip():
            wrapped = textwrap.fill(
                paragraph,
                width             = TERMINAL_WIDTH - 4,
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
    1. Validate the Gemini API key is set.
    2. Spawn the PowerBI MCP server subprocess.
    3. Perform MCP handshake + discover tools.
    4. Build the Gemini client and agent.
    5. Enter the interactive REPL.
    """

    # ── 1. Validate API key ─────────────────────────────────────────────────
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print("\n[ERROR] GEMINI_API_KEY environment variable is not set.")
        print("  Get your free key at: https://aistudio.google.com/app/apikey")
        sys.exit(1)

    # ── 2. Start the MCP server ─────────────────────────────────────────────
    print("\nStarting PowerBI MCP client (Gemini edition) …\n")
    server = MCPServerProcess(POWERBI_MCP_EXE, MCP_SERVER_ARGS)
    server.start()

    # ── 3. Discover tools ───────────────────────────────────────────────────
    print("  Fetching tool catalogue from MCP server …")
    tools = fetch_tools(server)   # Returns list[genai_types.Tool]

    # ── 4. Build Gemini client and agent ────────────────────────────────────
    # genai.Client automatically reads GEMINI_API_KEY from the environment.
    gemini_client = genai.Client(api_key=api_key)
    agent = PowerBIGeminiAgent(gemini_client, server, tools)

    print_welcome(tools)

    # ── 5. REPL ─────────────────────────────────────────────────────────────
    try:
        while True:
            try:
                user_input = input("  You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n\n  Goodbye!")
                break

            if not user_input:
                continue

            # ── Special slash-commands ──────────────────────────────────────
            if user_input.lower() in ("/quit", "/exit", "/q"):
                print("\n  Goodbye!")
                break

            if user_input.lower() == "/tools":
                all_decls = []
                for tool_obj in tools:
                    all_decls.extend(tool_obj.function_declarations or [])
                print(f"\n  {len(all_decls)} available MCP tools:")
                for decl in all_decls:
                    print(f"    • {decl.name}")
                    if decl.description:
                        print(f"      {decl.description[:100]}")
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
            except Exception as e:
                # Catches Gemini API errors (quota, auth, network, etc.)
                print(f"\n  [Error] {type(e).__name__}: {e}\n")

    finally:
        server.stop()
        print("  MCP server stopped. Bye!\n")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
