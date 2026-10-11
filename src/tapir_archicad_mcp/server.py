import argparse
import logging
import math
import os
import sys
from typing import Any

import tapir_archicad_mcp.model_configuration  # noqa: F401
from tapir_archicad_mcp.app import mcp
from tapir_archicad_mcp.command_guard import GuardSettings, parse_timeout_overrides
from tapir_archicad_mcp.jobs import JobSettings
from tapir_archicad_mcp.logging_config import setup_logging
from tapir_archicad_mcp.middleware import BearerTokenMiddleware

setup_logging()


def _finite_float(value: str, *, allow_zero: bool) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        requirement = "nonnegative" if allow_zero else "positive"
        raise argparse.ArgumentTypeError(f"Expected a finite {requirement} number.")
    return number


def _positive_float(value: str) -> float:
    return _finite_float(value, allow_zero=False)


def _nonnegative_float(value: str) -> float:
    return _finite_float(value, allow_zero=True)


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive integer.")
    return number


def _timeout_overrides(value: str) -> dict[str, float]:
    try:
        return parse_timeout_overrides(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Archicad Tapir MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="MCP transport to use. Defaults to stdio.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("TAPIR_MCP_HOST", "127.0.0.1"),
        help="Host to bind for HTTP-based transports.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("TAPIR_MCP_PORT", "8000")),
        help="Port to bind for HTTP-based transports.",
    )
    parser.add_argument(
        "--streamable-http-path",
        default=os.getenv("TAPIR_MCP_STREAMABLE_HTTP_PATH", "/mcp"),
        help="HTTP path for the streamable-http transport.",
    )
    parser.add_argument(
        "--mount-path",
        default=os.getenv("TAPIR_MCP_MOUNT_PATH", None),
        help="Optional mount path for SSE transport.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("TAPIR_MCP_TOKEN", None),
        help="Bearer token required for HTTP-based transports. Ignored for stdio.",
    )
    parser.add_argument(
        "--async-threshold-seconds", type=_positive_float,
        default=os.getenv("TAPIR_MCP_ASYNC_THRESHOLD_SECONDS", "45"),
        help="Maximum execution/poll wait before returning a running JobHandle (default: 45 seconds).",
    )
    parser.add_argument(
        "--job-ttl-seconds", type=_nonnegative_float,
        default=os.getenv("TAPIR_MCP_JOB_TTL_SECONDS", "86400"),
        help="Completed-job and page lifetime after completion; 0 disables age expiry (default: 86400).",
    )
    parser.add_argument(
        "--max-completed-jobs", type=_positive_int,
        default=os.getenv("TAPIR_MCP_MAX_COMPLETED_JOBS", "128"),
        help="Maximum retained completed/failed jobs; oldest are evicted with their pages (default: 128).",
    )
    parser.add_argument(
        "--command-timeout-seconds", type=_nonnegative_float,
        default=os.getenv("TAPIR_MCP_COMMAND_TIMEOUT_SECONDS", "60"),
        help="Time limit for one Archicad command; 0 waits without limit (default: 60).",
    )
    parser.add_argument(
        "--long-command-timeout-seconds", type=_nonnegative_float,
        default=os.getenv("TAPIR_MCP_LONG_COMMAND_TIMEOUT_SECONDS", "900"),
        help="Time limit for long-running commands such as opening projects or publishing; 0 waits without "
             "limit (default: 900).",
    )
    parser.add_argument(
        "--command-timeouts", type=_timeout_overrides,
        default=os.getenv("TAPIR_MCP_COMMAND_TIMEOUTS", ""),
        help="Per-command time limits as 'command=seconds,command=seconds'; overrides the two limits above.",
    )
    parser.add_argument(
        "--preflight-timeout-seconds", type=_nonnegative_float,
        default=os.getenv("TAPIR_MCP_PREFLIGHT_TIMEOUT_SECONDS", "5"),
        help="Time limit of the API.IsAlive check sent before each command; 0 disables the check (default: 5).",
    )
    parser.add_argument(
        "--dialog-grace-seconds", type=_nonnegative_float,
        default=os.getenv("TAPIR_MCP_DIALOG_GRACE_SECONDS", "10"),
        help="Stop waiting for a command once a modal dialog has blocked Archicad this long; 0 disables "
             "(default: 10).",
    )
    return parser.parse_args()


def sse_paths_from_mount_path(mount_path: str | None) -> dict[str, str]:
    """Preserve the legacy SSE prefix in v2's explicit routes."""
    if not mount_path or mount_path == "/":
        return {}

    prefix = "/" + mount_path.strip("/")
    return {
        "sse_path": f"{prefix}/sse",
        "message_path": f"{prefix}/messages/",
    }


def main():
    args = parse_args()
    import tapir_archicad_mcp.app as app_module

    app_module.job_settings = JobSettings(
        async_threshold_seconds=args.async_threshold_seconds,
        job_ttl_seconds=args.job_ttl_seconds,
        max_completed_jobs=args.max_completed_jobs,
    )
    app_module.guard_settings = GuardSettings(
        command_timeout_seconds=args.command_timeout_seconds,
        long_command_timeout_seconds=args.long_command_timeout_seconds,
        command_timeout_overrides=args.command_timeouts,
        preflight_timeout_seconds=args.preflight_timeout_seconds,
        dialog_grace_seconds=args.dialog_grace_seconds,
    )
    sys.argv = [sys.argv[0]]

    logging.info(
        "Starting Archicad Tapir MCP Server with transport=%s host=%s port=%s streamable_path=%s mount_path=%s",
        args.transport,
        args.host,
        args.port,
        args.streamable_http_path,
        args.mount_path
    )

    # MCPServer configures HTTP options on its transport methods.
    http_options: dict[str, Any] = {"host": args.host}
    if args.transport == "sse" and args.mount_path:
        http_options.update(sse_paths_from_mount_path(args.mount_path))
    elif args.transport == "streamable-http" and args.streamable_http_path:
        http_options["streamable_http_path"] = args.streamable_http_path

    if args.token and args.transport != "stdio":
        import uvicorn

        app = mcp.sse_app(**http_options) if args.transport == "sse" else mcp.streamable_http_app(**http_options)
        uvicorn.run(BearerTokenMiddleware(app, args.token), host=args.host, port=args.port)
        return

    if args.token:
        logging.warning("--token is only used for HTTP-based transports and is ignored for stdio.")

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, port=args.port, **http_options)

if __name__ == "__main__":
    main()
