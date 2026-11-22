"""Chat demo: a Databricks-hosted LLM uses the QHP PUF tools over the real MCP protocol.

The deployed Databricks App accepts only OAuth sign-ins and sits behind the workspace's Private
Link, so this demo connects an MCP client to the same server code through MCP's in-memory
transport. Every tool is still discovered with list_tools and run with call_tool messages.
"""

from __future__ import annotations

import json

from databricks.sdk import WorkspaceClient
from mcp.shared.memory import create_connected_server_and_client_session

from qhp_mcp.server import SERVER_INSTRUCTIONS, build_server

DEFAULT_MODEL = "databricks-meta-llama-3-3-70b-instruct"
MAX_TOOL_ROUNDS = 8
MAX_TOOL_TEXT_FOR_MODEL = 15000  # keeps large pulls within the model's reading limit

SYSTEM_PROMPT = SERVER_INSTRUCTIONS + (
    " You are assisting a government analyst. Use only tool results and never invent numbers. "
    "After prepare_data_request, show the plain_language_summary and every question to the user, then STOP "
    "and wait for their reply. Call pull_plan_data only after the user explicitly approves in a later message. "
    "After pulling, call verify_data_pull and report whether all checks passed and the evidence fingerprint."
)


class QhpChat:
    """A multi-turn chat whose only data access is the four MCP tools."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self.llm = WorkspaceClient().serving_endpoints.get_open_ai_client()
        self.server = build_server()
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    async def ask(self, user_text: str, show_steps: bool = True) -> str:
        self.messages.append({"role": "user", "content": user_text})
        # _mcp_server is FastMCP's underlying protocol server, which the in-memory transport connects to.
        async with create_connected_server_and_client_session(self.server._mcp_server) as session:
            listed = await session.list_tools()
            tools = [{"type": "function",
                      "function": {"name": t.name, "description": t.description, "parameters": t.inputSchema}}
                     for t in listed.tools]
            for _ in range(MAX_TOOL_ROUNDS):
                reply = self.llm.chat.completions.create(
                    model=self.model, messages=self.messages, tools=tools, temperature=0).choices[0].message
                if not reply.tool_calls:
                    self.messages.append({"role": "assistant", "content": reply.content or ""})
                    return reply.content or ""
                self.messages.append({"role": "assistant", "content": reply.content or "",
                                      "tool_calls": [call.model_dump() for call in reply.tool_calls]})
                for call in reply.tool_calls:
                    try:
                        arguments = json.loads(call.function.arguments or "{}")
                        result = await session.call_tool(call.function.name, arguments)
                        text = "".join(getattr(part, "text", "") for part in result.content)
                    except json.JSONDecodeError:
                        arguments, text = {}, '{"ok": false, "message": "Tool arguments were not valid JSON."}'
                    if show_steps:
                        print(f"  -> MCP call: {call.function.name}({json.dumps(arguments)[:250]})")
                        print(f"     <- {text[:250].replace(chr(10), ' ')}...")
                    self.messages.append({"role": "tool", "tool_call_id": call.id,
                                          "content": text[:MAX_TOOL_TEXT_FOR_MODEL]})
        return "Stopped after too many tool calls. Please rephrase the request."