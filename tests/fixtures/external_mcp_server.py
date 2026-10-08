import json
import os
from pathlib import Path
import time

from mcp.server.fastmcp import FastMCP
from mcp import types


if os.environ.get("EXTERNAL_MCP_DELAY"):
    time.sleep(float(os.environ["EXTERNAL_MCP_DELAY"]))

server = FastMCP("harmless-lab", instructions="Use temperatures as observations, not accepted conclusions.",
    host="127.0.0.1", port=int(os.environ.get("EXTERNAL_MCP_PORT", "9300")))


@server.tool(description="Read the fixture temperature in Celsius.")
def read_temperature(sensor: str) -> dict[str, object]:
    ledger = Path(os.environ["EXTERNAL_MCP_LEDGER"])
    with ledger.open("a") as stream:
        stream.write(json.dumps({"name": "read_temperature", "sensor": sensor}) + "\n")
    return {"sensor": sensor, "temperature": 23.75}


@server.tool(description="A business action. Never use this for connection testing.")
def connectivity_action() -> str:
    with Path(os.environ["EXTERNAL_MCP_LEDGER"]).open("a") as stream:
        stream.write('"connectivity_action"\n')
    return "business-action-recorded"


list_tools_handler = server._mcp_server.request_handlers[types.ListToolsRequest]


async def audited_list_tools(request):
    if request is None:
        return await list_tools_handler(request)
    audit_path = os.environ.get("EXTERNAL_MCP_AUDIT")
    if audit_path:
        with Path(audit_path).open("a") as stream:
            stream.write('"tools/list"\n')
    if os.environ.get("EXTERNAL_MCP_PAGINATE"):
        tools = (await list_tools_handler(request)).root.tools
        cursor = request.params.cursor if request.params else None
        return types.ServerResult(types.ListToolsResult(tools=tools[:1] if cursor is None else tools[1:],
            nextCursor="next-page" if cursor is None else None))
    return await list_tools_handler(request)


server._mcp_server.request_handlers[types.ListToolsRequest] = audited_list_tools
server.run(transport=os.environ.get("EXTERNAL_MCP_TRANSPORT", "stdio"))
