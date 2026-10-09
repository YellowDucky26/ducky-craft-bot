"""HTTP-server voor inkomende meldingen (Jellyseerr-webhook en generiek /notify)."""
import hmac
import logging
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from .bot import DuckyBot

log = logging.getLogger(__name__)


def authorized(request: web.Request, secret: str) -> bool:
    supplied = request.headers.get("Authorization", "")
    if supplied.lower().startswith("bearer "):
        supplied = supplied[7:]
    return bool(secret) and hmac.compare_digest(supplied.strip().encode(), secret.encode())


def create_app(bot: "DuckyBot") -> web.Application:
    secret = bot.config.webhook_secret

    async def read_json(request: web.Request) -> dict:
        if not authorized(request, secret):
            raise web.HTTPUnauthorized(text="ongeldige Authorization-header")
        try:
            payload = await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="body is geen geldige JSON")
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="verwacht een JSON-object")
        return payload

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "discord": bot.is_ready()})

    async def jellyseerr_webhook(request: web.Request) -> web.Response:
        payload = await read_json(request)
        cog = bot.get_cog("Notifications")
        notice = await cog.handle_seerr_webhook(payload)  # type: ignore[union-attr]
        return web.json_response({"ok": True, "type": notice.notification_type})

    async def notify(request: web.Request) -> web.Response:
        payload = await read_json(request)
        cog = bot.get_cog("Notifications")
        try:
            delivered = await cog.handle_notify(payload)  # type: ignore[union-attr]
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc))
        if not delivered:
            return web.json_response({"ok": False, "error": "afleveren in Discord mislukt"}, status=502)
        return web.json_response({"ok": True})

    app = web.Application(client_max_size=256 * 1024)
    app.router.add_get("/health", health)
    app.router.add_post("/webhook/jellyseerr", jellyseerr_webhook)
    app.router.add_post("/notify", notify)
    return app


async def start_web_server(bot: "DuckyBot") -> web.AppRunner:
    runner = web.AppRunner(create_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", bot.config.web_port).start()
    log.info("Webhook-server luistert op poort %d", bot.config.web_port)
    return runner
