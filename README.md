# mcp-guard

Asks you to allow or block every MCP tool call that Claude makes, before it
reaches the server. Works with Claude Desktop (local servers) and Claude Web
(remote connectors).

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

Python 3.10+.

```
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## Usage

Start the approval app and keep it running:

```
mcp-guard daemon
```

Guard Claude Desktop's servers. `--add-demo` also adds a small test server:

```
mcp-guard install --add-demo
mcp-guard status
```

Then fully quit Claude Desktop from the tray icon and open it again. Ask it to
"save a note with mcp-guard-demo" and the pop-up should appear.
`mcp-guard uninstall` puts the config back. A backup is made either way.

Claude Web:

```
python examples/demo_http_server.py --port 9000
mcp-guard http --upstream http://127.0.0.1:9000 --port 8787 --path-secret auto
cloudflared tunnel --url http://127.0.0.1:8787
```

Add `https://<tunnel>/<secret>/mcp` as a custom connector on claude.ai.

To try the pop-up without Claude: `mcp-guard test-popup --count 3`

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
