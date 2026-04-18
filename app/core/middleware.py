from starlette.datastructures import MutableHeaders

from app.core.config import get_settings
from app.core.http import scope_is_secure


class TransportMiddleware:
    """
    Normalize transport behavior for browser and probe traffic.

    - HEAD requests should work anywhere GET works so `curl -I` and basic
      uptime probes don't fail with 405.
    - Production HTTPS responses should advertise baseline trust headers.
    """

    def __init__(self, app):
        self.app = app
        self.settings = get_settings()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_head = scope["method"] == "HEAD"
        downstream_scope = dict(scope)
        if is_head:
            downstream_scope["method"] = "GET"

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message["headers"])
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault(
                    "Permissions-Policy",
                    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), microphone=(), payment=(), usb=()",
                )
                if self.settings.app_env == "production" and scope_is_secure(scope):
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            elif is_head and message["type"] == "http.response.body":
                message = {**message, "body": b""}

            await send(message)

        await self.app(downstream_scope, receive, send_wrapper)
