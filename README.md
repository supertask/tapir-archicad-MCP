# Archicad Tapir MCP Server

This project provides a Model Context Protocol (MCP) server for Archicad. It acts as a bridge, allowing AI agents and applications (like Claude for Desktop) to interact with running Archicad instances by wrapping both the community-driven **Tapir API** and the **official Archicad JSON API**.

The server dynamically generates a comprehensive set of **191+** MCP tools from the combined API schemas, enabling fine-grained control over Archicad projects.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)]()

## Key Features

-   **Pydantic-Powered Runtime Validation & Schema Generation:** Every tool's input and output shapes are backed by rigorous Pydantic models. The server dynamically compiles these into detailed JSON Schemas for the AI agent to inspect, and strictly validates all incoming tool arguments at runtime *before* forwarding them to Archicad. It handles complex models, Union types, and TypeAliases seamlessly.
-   **Progressive Tool Discovery (CLI-style):** The server uses a deterministic workflow (`archicad_list_commands` and `archicad_get_command_schema`) that allows AI agents to list available commands and fetch exact parameter schemas on demand. This avoids flooding the model's context window with large schemas.
-   **No Heavy Machine-Learning Dependencies:** Vector-based search has been removed. The server no longer requires heavy packages like PyTorch, `faiss-cpu`, or `sentence-transformers`, dramatically reducing the package size and eliminating server startup delays.
-   **Massive Toolset, Minimal Footprint:** Provides access to a unified toolset of **191+ commands** by merging the community Tapir API and the official Archicad JSON API.
-   **Flexible Network Transports:** Supports `sse` (Server-Sent Events) and `streamable-http` transports in addition to standard input/output (`stdio`), allowing the server to be run on remote host configurations.
-   **Bearer Token Authentication:** Secures HTTP endpoints when using SSE or Streamable-HTTP via an opt-in token validation middleware (`--token` flag or `TAPIR_MCP_TOKEN` environment variable).
-   **Multi-Instance Control:** Connect to and manage multiple running Archicad instances simultaneously, targeting commands to specific instances via port numbers.
-   **Cross-Platform Support:** Compatible with both Windows and macOS systems.

## Installation & Setup

Follow these steps to get the server running and connected to an MCP client like Claude for Desktop.

### 1. Prerequisites

-   **Python 3.12+** and **`uv`**: Ensure you have a modern version of Python and the `uv` package manager installed.
-   **Archicad & Tapir Add-On**: You must have Archicad running (which includes the official JSON API). To access the full set of community-developed tools, the [Tapir Archicad Add-On](https://github.com/ENZYME-APD/tapir-archicad-automation) must also be installed.
-   **MCP Client**: An application that can host MCP servers, such as [Claude for Desktop](https://www.claude.ai/download) or [Gemini CLI](https://github.com/google-gemini/gemini-cli).

### 2. Configure Your AI Client

Open your client's `config.json` file and add the following configuration. This command works across operating systems:

-   **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
-   **Windows:** `%APDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "ArchicadTapir": {
      "command": "uvx",
      "args": [
        "--from",
        "tapir-archicad-mcp",
        "archicad-server"
      ]
    }
  }
}
```

### 3. One-click install (optional)

If your client supports [MCP Bundles](https://github.com/modelcontextprotocol/mcpb) you can skip the config file above. Claude Desktop, Claude Code and MCP for Windows all install `.mcpb` files.

Download [tapir-archicad.mcpb](https://github.com/SzamosiMate/tapir-archicad-MCP/releases/latest/download/tapir-archicad.mcpb) from the latest release and open it in your client. On the Microsoft Store build of Claude Desktop this is the easier route, because the app does not read the `%APPDATA%\Claude\claude_desktop_config.json` that everyone documents.

`uv` still has to be on the PATH your client sees, but it provisions Python and the locked dependencies itself, so there is nothing else to install. The first launch takes 15 to 30 seconds while it builds that environment. The bundle can modify the open project, export files, and send or receive Teamwork, so treat installing it like handing your client your Archicad seat.

<details>
<summary>If the bundle will not start</summary>

-   **"Server disconnected", or the install itself fails.** Uninstall the bundle, restart the client, then install it again. Installing over an existing bundle can leave stale state behind that a straight reinstall does not clear.
-   **Nothing happens for the first half minute.** That is uv building the environment, and antivirus scanning or a slow connection stretches it out. Restart the client once before assuming the bundle is broken.
-   **Claude Desktop logs** are at `%APPDATA%\Claude\logs\mcp-server-Archicad (Tapir).log`. On the Microsoft Store build that path is redirected and the real file is `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\logs\mcp-server-Archicad (Tapir).log`.

</details>

Icon artwork is the official Tapir mark from [ENZYME-APD/tapir-archicad-automation](https://github.com/ENZYME-APD/tapir-archicad-automation) (`branding/logo/png/tapir_logo_black_512.png`), MIT, Copyright 2024 Enzyme APD.

## Configuration Options

You can customize the server via CLI flags or environment variables:

| CLI Flag | Environment Variable | Default | Description |
| :--- | :--- | :--- | :--- |
| `--transport` | - | `stdio` | Transport protocol to use (`stdio`, `sse`, or `streamable-http`) |
| `--host` | `TAPIR_MCP_HOST` | `127.0.0.1` | Bind address for HTTP-based transports |
| `--port` | `TAPIR_MCP_PORT` | `8000` | Bind port for HTTP-based transports |
| `--token` | `TAPIR_MCP_TOKEN` | `None` | Optional Bearer token to secure HTTP endpoints |
| `--async-threshold-seconds` | `TAPIR_MCP_ASYNC_THRESHOLD_SECONDS` | `45` | Seconds to wait before returning a running job handle |
| `--job-ttl-seconds` | `TAPIR_MCP_JOB_TTL_SECONDS` | `86400` | Seconds to keep finished jobs; `0` disables time-based expiry |
| `--max-completed-jobs` | `TAPIR_MCP_MAX_COMPLETED_JOBS` | `128` | Maximum number of finished jobs to keep |
| `--command-timeout-seconds` | `TAPIR_MCP_COMMAND_TIMEOUT_SECONDS` | `60` | Time limit for one Archicad command; `0` waits without limit |
| `--long-command-timeout-seconds` | `TAPIR_MCP_LONG_COMMAND_TIMEOUT_SECONDS` | `900` | Time limit for long-running commands (opening, saving, publishing, IFC, Teamwork…) |
| `--command-timeouts` | `TAPIR_MCP_COMMAND_TIMEOUTS` | - | Per-command limits as `command=seconds,command=seconds`, e.g. `project_open_project=1800` |
| `--preflight-timeout-seconds` | `TAPIR_MCP_PREFLIGHT_TIMEOUT_SECONDS` | `5` | Time limit of the `API.IsAlive` check sent before each command; `0` disables it |
| `--dialog-grace-seconds` | `TAPIR_MCP_DIALOG_GRACE_SECONDS` | `10` | Stop waiting once a modal dialog has blocked Archicad this long; `0` disables |

CLI flags override environment variables.

## Long-running commands

Commands normally return their result directly. If a command takes longer than 45 seconds, `archicad_call_tool` returns a job handle while execution continues:

```json
{"status": "running", "jobHandle": "job:83af21..."}
```

Call `archicad_get_job(job_handle="job:83af21...")` to check progress and retrieve the result. Repeat while the status is `running`; the final status is `completed` or `failed`. If your client times out sooner, lower `--async-threshold-seconds`.

If a response is lost, call `archicad_get_job()` to list recent jobs before repeating a command that changes the project. Cancelling a request does not stop a command already started, and retrying it may duplicate changes.

Jobs remain available across HTTP client reconnects while the server is running, but are lost on server restart. Retention defaults to 24 hours and a maximum of 128 finished jobs.

## Modal dialogs, time limits and duplicate protection

While Archicad shows a modal dialog (for example *Missing Add-Ons* after opening a project) it does not process API commands: they wait in its queue and run when the dialog is closed. The server guards against this:

- **Preflight.** Before each command the server sends `API.IsAlive`, which Archicad answers even behind a dialog. If a dialog is open, the call fails immediately with the dialog's title, text and buttons, and the command is **not sent**.
- **Time limits.** Every command has a limit (60 s by default, 900 s for long-running commands such as `project_open_project` or `navigator_publish_publisher_set`; see the options above). If a blocking dialog appears while a command runs, the server stops waiting after `--dialog-grace-seconds`. The error says Archicad is not responding and lists the dialogs found.
- **Pending commands.** A command abandoned this way is still queued inside Archicad. The server records it, keeps listening, and records whether it later succeeded or failed (`archicad_list_pending_commands`). An identical write command (same name, port and parameters) is refused while such a record exists; pass `allow_duplicate=true` to `archicad_call_tool` to send it anyway, or forget the record with `archicad_clear_pending_commands`. Read commands (`*_get_*`, `*_list_*`…) are never refused.
- **`archicad_get_dialogs`** lists the modal dialogs each Archicad instance is showing. It never presses a button. Dialog inspection uses Win32 and is available on Windows only; elsewhere the preflight still reports the dialog name Archicad gives.

## Usage

1. **Restart Claude for Desktop** to apply configuration changes.
2. Ensure at least one instance of Archicad is running.
3. The client will initially have access to a small set of core tools. Start by asking the AI to find running Archicad instances:

   > "Can you check what Archicad projects I have running?"

   The AI will call `discovery_list_active_archicads` and report the active instances and their `port` numbers.

4. State your main goal:

   > "Using port 19723, get all the Wall elements from the project."

5. The AI will execute a progressive discovery and calling loop:
   - **Step 1:** It queries `archicad_list_commands` to look up the correct command name for the requested action (identifying `elements_get_elements_by_type`).
   - **Step 2:** It calls `archicad_get_command_schema` with the target command name to retrieve the exact required JSON parameter structure.
   - **Step 3:** It calls `archicad_call_tool` with the command name, the targeted `port`, and the required parameter payload.

## How It Works

The server operates through a layered architecture:

-   **AI Agent (e.g., Claude):** Interprets user prompts and orchestrates tool discovery.
-   **MCP Client (e.g., Claude for Desktop):** Manages the server process and handles communication.
-   **MCP Server (This Project):** Standardizes tool descriptions, parameters, and results, presenting a clean `list`/`schema`/`call` interface.
-   **`multiconn_archicad` Library:** Resolves active socket connections and handles low-level command dispatch to Archicad instances.
-   **Archicad & Tapir Add-On:** Built-in APIs and Tapir Add-on execute commands and return structured data.

## Contributing

Contributions are welcome! Please feel free to submit an issue or open a pull request.

## License

This project is licensed under the MIT License. See the [LICENSE](./LICENSE) file for details.
