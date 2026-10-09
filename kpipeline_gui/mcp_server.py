#!/usr/bin/env python3
"""
kpipeliner MCP server — headless, no GUI/REST API/static frontend at all.

Just the MCP endpoint (streamable-HTTP, at /mcp) for the pipeline in
--workdir, run directly as uvicorn's root ASGI app: leaner than mounting
into the full GUI server (see mcp_app.mount(), used by `kpl gui --mcp`
instead) when nobody wants the browser-facing GUI running at all.

Launched by `kpl mcp` (kpipeline.py Pipeliner.run_mcp()) — not meant to be
run directly, though it can be (same --workdir/--host/--port args as
kpipeline_gui/server.py).
"""

import argparse
import os
import socket
import sys

import uvicorn

GUI_DIR = os.path.dirname(os.path.abspath(__file__))
PIPELINER_DIR = os.path.dirname(GUI_DIR)
sys.path.insert(0, PIPELINER_DIR)

try:
    from kpipeline_gui.mcp_app import build_server  # noqa: E402
except ImportError:
    print("MCP support needs the optional 'mcp' package (not installed).")
    print(f'Install it with: {sys.executable} -m pip install -r '
          f"{os.path.join(PIPELINER_DIR, 'requirements-mcp.txt')}")
    sys.exit(1)


def _find_free_port(host, start, tries=100):
    for port in range(start, start + tries):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
            return port
        except OSError:
            continue
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--workdir', default=os.getcwd())
    p.add_argument('--host', default='0.0.0.0')
    p.add_argument('--port', type=int, default=8601)
    args = p.parse_args()

    workdir = os.path.abspath(args.workdir)
    os.chdir(workdir)

    port = _find_free_port(args.host, args.port)
    if port is None:
        print(f'No free port found in range {args.port}-{args.port + 99}.')
        sys.exit(1)
    if port != args.port:
        print(f'Port {args.port} is in use - using port {port} instead.')

    local_host = 'localhost' if args.host == '0.0.0.0' else args.host
    print(f'kpipeliner MCP → http://{local_host}:{port}/mcp')
    print(f'Working directory: {workdir}')

    mcp = build_server(workdir)
    app = mcp.streamable_http_app()
    uvicorn.run(app, host=args.host, port=port, log_level='warning')


if __name__ == '__main__':
    main()
