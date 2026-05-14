"""WebSocket Gateway — WSS + HTTP прокси на локальный relay"""
import asyncio, json, logging, sys
from aiohttp import web, WSMsgType, ClientSession, ClientTimeout
import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("relay_gateway")

LOCAL = "http://127.0.0.1:8198"

async def proxy_handler(request):
    """Прокси HTTP-запросы на локальный relay + проброс WS"""
    # Проверка WebSocket upgrade
    if request.headers.get('Upgrade', '').lower() == 'websocket':
        return await ws_proxy(request)
    return await http_proxy(request)

async def http_proxy(request):
    """Прокси HTTP запросы на локальный relay"""
    try:
        # Собираем путь и query
        path = request.path_qs or '/'
        headers = dict(request.headers)
        # Убираем hop-by-hop заголовки
        for h in ['Upgrade', 'Connection', 'Proxy-Connection', 'Transfer-Encoding']:
            headers.pop(h, None)
        
        body = await request.read()
        
        async with ClientSession() as session:
            url = f"{LOCAL}{path}"
            async with session.request(
                request.method, url,
                headers=headers,
                data=body or None,
                timeout=ClientTimeout(total=30)
            ) as resp:
                resp_headers = dict(resp.headers)
                # Убираем Transfer-Encoding если есть Content-Length
                if resp_headers.get('Content-Length') and resp_headers.get('Transfer-Encoding'):
                    resp_headers.pop('Transfer-Encoding', None)
                
                resp_body = await resp.read()
                return web.Response(
                    status=resp.status,
                    headers=resp_headers,
                    body=resp_body
                )
    except Exception as e:
        logger.error(f"HTTP proxy error: {e}")
        return web.json_response({"error": "Backend temporarily unavailable"}, status=502)

async def ws_proxy(request):
    """Прокси WebSocket на локальный relay"""
    logger.info(f"WS incoming: {request.remote}")
    try:
        async with ClientSession() as session:
            async with session.ws_connect(f"{LOCAL}/") as local_ws:
                resp = web.WebSocketResponse()
                await resp.prepare(request)
                
                async def relay_to_client():
                    try:
                        async for msg in local_ws:
                            if msg.type == WSMsgType.TEXT:
                                await resp.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                await resp.send_bytes(msg.data)
                            elif msg.type == WSMsgType.CLOSED:
                                break
                    except:
                        pass
                
                async def client_to_relay():
                    try:
                        async for msg in resp:
                            if msg.type == WSMsgType.TEXT:
                                await local_ws.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                await local_ws.send_bytes(msg.data)
                            elif msg.type == WSMsgType.CLOSED:
                                break
                    except:
                        pass
                
                await asyncio.gather(relay_to_client(), client_to_relay())
                return resp
    except Exception as e:
        logger.error(f"WS proxy error: {e}")
        return web.Response(status=502, text="WebSocket proxy failed")

async def main():
    app = web.Application()
    # Все запросы идут через proxy_handler (и HTTP, и WS)
    app.router.add_route("*", "/{path:.*}", proxy_handler)
    
    logger.info("Gateway listening on 0.0.0.0:9900")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 9900)
    await site.start()
    
    try:
        await asyncio.Event().wait()
    except:
        pass

if __name__ == "__main__":
    asyncio.run(main())
