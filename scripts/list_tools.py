import asyncio, sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SERVERS = {"metadata": 8001, "bsl": 8002, "help": 8003, "query": 8009}
# v8std — чужой сервер (STD-4), нашего bearer не знает: ходим без него.
NO_AUTH = {"v8std"}
SERVERS["v8std"] = 8765

def secret():
    for line in open(".env", encoding="utf-8-sig"):
        if line.startswith("MCP_SHARED_SECRET="):
            return line.split("=", 1)[1].strip()
    sys.exit("MCP_SHARED_SECRET не найден в .env")

async def one(name, port, s):
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        headers = {} if name in NO_AUTH else {"Authorization": f"Bearer {s}"}
        async with streamablehttp_client(url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                tools = sorted(t.name for t in (await sess.list_tools()).tools)
                print(f"\n{name} ({port}) — {len(tools)}")
                for t in tools:
                    print("   ", t)
                return len(tools)
    except Exception as e:
        print(f"\n{name} ({port}) — ОШИБКА: {type(e).__name__}: {e}")
        return 0

async def main():
    s = secret()
    total = sum([await one(n, p, s) for n, p in SERVERS.items()])
    print(f"\nИТОГО: {total}")

asyncio.run(main())