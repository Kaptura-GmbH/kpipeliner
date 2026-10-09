"""
MCP tool definitions for kpipeliner, shared by both ways of running them:
  - standalone, no GUI at all (kpipeline_gui/mcp_server.py, `kpl mcp`)
  - mounted onto the GUI's FastAPI app (`kpl gui --mcp`), so an LLM client
    can use the same port as the human GUI/REST API

Tool bodies live in kpipeline.src.kmcp (transport-agnostic); this module
only adapts them to the MCP SDK.
"""

import contextlib

from mcp.server.mcpserver import MCPServer
from starlette.routing import Mount, Route

from kpipeline.src import kmcp


def build_server(workdir: str) -> MCPServer:
    mcp = MCPServer(
        'kpipeliner',
        instructions=(
            'Design, inspect, and run the kpipeliner pipeline in this '
            'server\'s working directory. Node connections are plain '
            'strings of the form "nodes.<node_name>.<output_name>" set as '
            'an input value via set_node_input. Call get_graph and '
            'list_node_types before editing to see current state and '
            'available node types.'
        ),
    )

    @mcp.tool()
    def init_pipeline() -> dict:
        """Initialize a new pipeline (graph_config.yaml) in the working directory."""
        return kmcp.init_pipeline(workdir)

    @mcp.tool()
    def list_node_types() -> list:
        """List available node types (type, category, inputs, outputs, description)."""
        return kmcp.list_node_types(workdir)

    @mcp.tool()
    def get_graph() -> dict:
        """Return the current pipeline graph: nodes, their inputs/outputs, and the declared pipeline inputs/outputs."""
        return kmcp.get_graph(workdir)

    @mcp.tool()
    def add_node(node_type: str, node_id: str | None = None) -> dict:
        """Add a node of `node_type` to the graph (auto-named if node_id is omitted)."""
        return kmcp.add_node(workdir, node_type, node_id)

    @mcp.tool()
    def remove_node(node_name: str) -> dict:
        """Remove a node from the graph and clear any dangling references to it."""
        return kmcp.remove_node(workdir, node_name)

    @mcp.tool()
    def set_node_input(node_name: str, input_name: str, value) -> dict:
        """Set a node input to a literal value, or a "nodes.<node_name>.<output_name>" string to connect it to another node's output."""
        return kmcp.set_node_input(workdir, node_name, input_name, value)

    @mcp.tool()
    def set_pipeline_io(inputs: list | None = None, outputs: list | None = None) -> dict:
        """Set the pipeline's declared input/output node names. `outputs` controls what `run_pipeline` (with no node_id) actually executes."""
        return kmcp.set_pipeline_io(workdir, inputs, outputs)

    @mcp.tool()
    def run_pipeline(node_id: str | None = None, debug: bool = False, use_cache: bool = True) -> dict:
        """Run the pipeline (or a single node), blocking until it finishes; returns combined stdout/stderr."""
        return kmcp.run_pipeline(workdir, node_id, debug, use_cache)

    @mcp.tool()
    def get_status() -> dict:
        """Show cache/run status for every node."""
        return kmcp.get_status(workdir)

    @mcp.tool()
    def get_tree() -> dict:
        """Print the graph's dependency tree."""
        return kmcp.get_tree(workdir)

    @mcp.tool()
    def create_node_type(node_type: str, category: str | None = None, description: str = '') -> dict:
        """Create a new node type's node.py + node_config.yaml from the standard templates, local to this pipeline only."""
        return kmcp.create_node_type(workdir, node_type, category, description)

    @mcp.tool()
    def read_node_file(node_type: str, filename: str = 'node.py') -> dict:
        """Read a node type's node.py or node_config.yaml (filename: 'node.py' or 'node_config.yaml')."""
        return kmcp.read_node_file(workdir, node_type, filename)

    @mcp.tool()
    def write_node_file(node_type: str, filename: str, content: str) -> dict:
        """Overwrite a node type's node.py or node_config.yaml. Only works for node types local to this pipeline (its own custom_base_nodes_dirs) — shared/default base nodes are read-only here."""
        return kmcp.write_node_file(workdir, node_type, filename, content)

    return mcp


class _BarePathDispatch:
    """Serves `path` itself (no trailing slash) as `asgi_app`'s root.

    A plain Starlette `Mount("/mcp", ...)` only matches "/mcp/..." (it
    requires the trailing slash) — a bare "/mcp" request normally falls
    through to FastAPI's own redirect-slash handling, but server.py's SPA
    catch-all route (`GET /{full_path:path}`) intercepts first as a 405
    "partial match" for any non-GET method, short-circuiting that redirect.
    Registered as its own exact-path Route so both forms work directly,
    without touching server.py's existing routing.

    Not a plain function: Starlette's Route treats function/method endpoints
    as `func(request) -> response` and only calls class instances as raw
    ASGI, which is what a mounted sub-app expects here.
    """

    def __init__(self, asgi_app):
        self._asgi_app = asgi_app

    async def __call__(self, scope, receive, send):
        scope = {**scope, 'path': '/'}
        await self._asgi_app(scope, receive, send)


def mount(app, workdir: str, path: str = '/mcp') -> None:
    """Mount an MCP streamable-HTTP endpoint at `path` on `app`, operating
    on `workdir`. Combines the MCP session manager's lifespan with
    whatever lifespan `app` already had, since a mounted sub-app's own
    ASGI lifespan otherwise never runs."""
    mcp = build_server(workdir)
    mcp_asgi = mcp.streamable_http_app(streamable_http_path='/')
    # Inserted at the front, not appended: server.py registers a catch-all
    # GET '/{full_path:path}' route (SPA fallback) at import time, which
    # would otherwise shadow a later-added '/mcp' mount for every method.
    app.router.routes.insert(0, Mount(path, app=mcp_asgi))
    app.router.routes.insert(0, Route(path, endpoint=_BarePathDispatch(mcp_asgi),
                                       methods=['GET', 'POST', 'DELETE']))

    previous_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def combined_lifespan(app):
        async with mcp.session_manager.run():
            async with previous_lifespan(app):
                yield

    app.router.lifespan_context = combined_lifespan
