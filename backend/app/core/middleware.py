"""Middlewares: cabeçalhos de segurança, request-id, rate limiting."""
from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import settings
from app.core.ratelimit import limiter

CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://connect.facebook.net",
    "style-src 'self'",
    "img-src 'self' data: blob: https://www.facebook.com",
    "font-src 'self'",
    "connect-src 'self' ws: wss: https://graph.facebook.com https://www.facebook.com",
    "frame-src https://www.facebook.com https://web.facebook.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        response: Response = await call_next(request)
        h = response.headers
        h["X-Request-ID"] = rid
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "strict-origin-when-cross-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"  # permite popup do Embedded Signup
        h["Cross-Origin-Resource-Policy"] = "same-site"
        # O Swagger (/docs) usa CDN; mantém CSP estrita nas demais rotas.
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi")):
            h["Content-Security-Policy"] = CSP
        if settings.enable_hsts or settings.is_prod:
            h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        if request.url.path.startswith("/api/"):
            h["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/"):
            h["Cache-Control"] = "no-cache"  # revalida por ETag: após um deploy o navegador não serve JS/CSS antigos
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith(("/api/", "/webhooks/")):
            ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?")
            is_auth = path.startswith("/api/v1/auth/")
            limit = settings.rate_limit_auth_per_minute if is_auth else settings.rate_limit_per_minute
            if path.startswith("/webhooks/"):
                limit = settings.rate_limit_per_minute * 20  # Meta pode enviar rajadas; autenticação é por assinatura HMAC
            ok, remaining = await limiter.hit(f"{'auth' if is_auth else 'api'}:{ip}", limit)
            if not ok:
                return JSONResponse(
                    {"error": {"code": "rate_limited", "message": "Muitas requisições. Tente novamente em instantes."}},
                    status_code=429, headers={"Retry-After": "30"},
                )
        return await call_next(request)
