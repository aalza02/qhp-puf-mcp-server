"""Databricks Apps entry point: starts the QHP PUF MCP server (Streamable HTTP at /mcp)."""

from qhp_mcp.server import main

if __name__ == "__main__":
    main()