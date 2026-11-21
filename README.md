# mcp-guard

Asks you to allow or block every MCP tool call that Claude makes, before it
reaches the server. Works with Claude Desktop (local servers) and Claude Web
(remote connectors).

It's a standalone desktop app written in Python. No browser extension, plugin or
add-on is used or needed.

Demo video: _link here_

## How it works

To actually stop a request you have to be in its path, so there are two proxies
plus one background app that shows the pop-ups.

**Claude Desktop** runs each MCP server as a child process and talks JSON-RPC
over stdin/stdout. `mcp-guard install` changes each entry in
`claude_desktop_config.json` so Claude starts the proxy, and the proxy starts
the real server:

```
Claude Desktop <-> mcp-guard stdio <-> real server
```

The proxy passes everything through untouched except `tools/call`, which it
holds while it asks the daemon. Allow sends the original message to the
server. Block sends Claude a tool error ("Request blocked by MCP Guard"), and
the server never sees the call.

**Claude Web** calls MCP servers from Anthropic's cloud, not from your
machine, so nothing local can catch those calls. Instead `mcp-guard http` runs
as a reverse proxy in front of the server, and you add the proxy's URL as the
connector.

**The daemon** (`mcp-guard daemon`) is a small Tk app. Proxies talk to it over
`127.0.0.1` with a token, using a long-poll that returns once you click. Each
request gets its own always-on-top pop-up, so several can be open at once and
answered in any order.

Some behaviour worth knowing:
- If the daemon isn't running, gated calls are blocked (fail closed).
- No answer within 55 s means block. Claude gives up at about 60 s anyway.
- If Claude cancels a request, its pop-up closes on its own.
- Every decision goes into `~/.mcp_guard/audit.jsonl`.

## Setup

You need Python 3.10+, Git and the Claude desktop app.

1. Clone the repo

   ```
   git clone https://github.com/trishitchar/mcp-guard.git
   cd mcp-guard
   ```

2. Create a virtual environment

   ```
   python -m venv .venv
   ```

3. Activate it (do this in every new terminal)

   ```
   .venv\Scripts\activate
   ```

   On macOS/Linux: `source .venv/bin/activate`

4. Install

   ```
   pip install -e ".[dev]"
   ```

5. Run the tests (optional)

   ```
   pytest
   ```

## Usage: Claude Desktop

1. Start the approval app and leave this terminal open

   ```
   mcp-guard daemon
   ```

2. In a second terminal (activate first), add the demo server and guard all servers

   ```
   mcp-guard install --add-demo
   ```

3. Check it

   ```
   mcp-guard status
   ```

   You should see `Daemon: RUNNING` and `[guarded] mcp-guard-demo`.

4. Fully quit Claude Desktop (tray icon → Quit) and open it again.

5. Open the Chat tab and type:
   `Use mcp-guard-demo to save a note titled hello with the text hi`.
   The pop-up appears. Click Allow.

6. Type: `Use mcp-guard-demo to delete all my notes`. Click Block.
   Claude says the request was blocked.

7. See which tools actually ran (the blocked one won't be there)

   ```
   Get-Content $HOME\.mcp_guard\demo_executions.log
   ```

Try the pop-up without Claude:

```
mcp-guard test-popup --count 3
```

## Usage: Claude Web

Each command runs in its own terminal (activate first).

1. Start the approval app

   ```
   mcp-guard daemon
   ```

2. Start the demo MCP server

   ```
   python examples/demo_http_server.py --port 9000
   ```

3. Start the guard proxy. Note the secret it prints in the URL.

   ```
   mcp-guard http --upstream http://127.0.0.1:9000 --port 8787 --path-secret auto
   ```

4. Get a public HTTPS address. Note the `https://....trycloudflare.com` it prints.

   ```
   cloudflared tunnel --url http://127.0.0.1:8787
   ```

5. On claude.ai go to Settings → Connectors → Add custom connector, and use
   `https://<tunnel>/<secret>/mcp`. On Team/Enterprise plans an admin has to add it.

6. In a chat, turn the connector on and ask it to use a tool. The pop-up appears on your PC.

## Uninstall

1. Restore Claude's config

   ```
   mcp-guard uninstall
   ```

2. Quit MCP Guard and restart Claude Desktop.

## Config

Optional, in `~/.mcp_guard/config.json`:

```json
{
  "decision_timeout_s": 55,
  "fail_mode": "closed",
  "gated_methods": ["tools/call"],
  "rules": [{"match": "mcp-guard-demo:get_time", "action": "allow"}]
}
```

Rules match `server:tool` globs and the first match wins. `mcp-guard config`
shows the current values.

## Tests

```
pytest
```

The tests run the real proxy and demo server as subprocesses, with a scripted
"user" in place of the pop-up. They cover allow, block, concurrent requests,
timeout, cancellation, daemon down, and the HTTP proxy.

## Limitations

- Desktop extensions (.mcpb) aren't listed in `claude_desktop_config.json`, so
  `install` can't wrap them.
- Built-in claude.ai connectors (Gmail, Drive...) go straight from Anthropic to
  Google, so they can't be intercepted.
