"""Allow ``python -m mcp_guard ...`` (this is what Claude Desktop launches)."""

from mcp_guard.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
