"""Read the public progressive catalog without assuming a provider tool layout."""


def catalog_operations(dispatch, token, **filters):
    arguments = {"view": "operations", "limit": 100, **filters}
    items = []
    while True:
        status, response = dispatch(token, {"jsonrpc": "2.0", "id": "catalog",
            "method": "tools/call", "params": {"name": "tools.catalog.page", "arguments": arguments}})
        assert status == 200, response
        result = response["result"]
        assert result["isError"] is False, result
        page = result["structuredContent"]
        items.extend(page["items"])
        if page["next_offset"] is None:
            return items
        arguments.update(offset=page["next_offset"], catalog_hash=page["catalog_hash"])


def catalog_tools(dispatch, token):
    tools = []
    for item in catalog_operations(dispatch, token):
        status, response = dispatch(token, {"jsonrpc": "2.0", "id": "contract",
            "method": "tools/call", "params": {"name": "tools.contracts.read",
                "arguments": {"operation_id": item["operation_id"]}}})
        assert status == 200, response
        assert response["result"]["isError"] is False, response
        tools.append(response["result"]["structuredContent"])
    return tools
