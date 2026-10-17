"""Cliente da WhatsApp Cloud API / Graph API (Meta).

* ``live``    → chamadas reais ao Graph API.
* ``sandbox`` → não toca a Meta: devolve identificadores plausíveis (wamid fictício) para desenvolvimento,
                demonstração e testes automatizados. O restante do sistema é idêntico nos dois modos.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger("flowmentor.meta")


class MetaError(Exception):
    def __init__(self, message: str, code: int | None = None, status: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.code, self.status, self.payload = code, status, payload or {}

    @property
    def retryable(self) -> bool:
        return self.status in (429, 500, 502, 503, 504) or self.code in (4, 80007, 130429, 131056)


class MetaClient:
    def __init__(self, access_token: str | None = None, phone_number_id: str | None = None, waba_id: str | None = None):
        self.token, self.phone_number_id, self.waba_id = access_token, phone_number_id, waba_id
        self.live = settings.meta_mode == "live"
        self.base = f"https://graph.facebook.com/{settings.meta_graph_version}"

    # ---------------------------------------------------------------- infra
    async def _request(self, method: str, path: str, *, json: Any = None, params: dict | None = None, data: Any = None, files: Any = None,
                       token: str | None = None, absolute: bool = False, raw: bool = False) -> Any:
        url = path if absolute else f"{self.base}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {token or self.token}"} if (token or self.token) else {}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, json=json, params=params, data=data, files=files, headers=headers)
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
            except Exception:  # noqa: BLE001
                err = {}
            raise MetaError(err.get("message") or resp.text[:300], err.get("code"), resp.status_code, err)
        return resp.content if raw else resp.json()

    @staticmethod
    def _fake_id(prefix: str = "wamid") -> str:
        return f"{prefix}.SANDBOX{uuid.uuid4().hex[:24].upper()}"

    # ---------------------------------------------------------------- mensagens
    async def send_message(self, to: str, payload: dict) -> str:
        """``payload`` já no formato da Cloud API (type + corpo). Retorna o wamid."""
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **payload}
        if not self.live:
            log.info("[sandbox] send_message to=%s type=%s", to[-4:], payload.get("type"))
            return self._fake_id()
        res = await self._request("POST", f"{self.phone_number_id}/messages", json=body)
        return res["messages"][0]["id"]

    async def send_text(self, to: str, text: str, preview_url: bool = False) -> str:
        return await self.send_message(to, {"type": "text", "text": {"body": text, "preview_url": preview_url}})

    async def send_template(self, to: str, name: str, language: str, components: list[dict] | None = None) -> str:
        tpl: dict[str, Any] = {"name": name, "language": {"code": language}}
        if components:
            tpl["components"] = components
        return await self.send_message(to, {"type": "template", "template": tpl})

    async def send_media(self, to: str, media_type: str, link: str, caption: str | None = None, filename: str | None = None) -> str:
        obj: dict[str, Any] = {"link": link}
        if caption and media_type in ("image", "video", "document"):
            obj["caption"] = caption
        if filename and media_type == "document":
            obj["filename"] = filename
        return await self.send_message(to, {"type": media_type, media_type: obj})

    async def send_interactive_buttons(self, to: str, body: str, buttons: list[tuple[str, str]], header: str | None = None, footer: str | None = None) -> str:
        inter: dict[str, Any] = {"type": "button", "body": {"text": body[:1024]},
                                 "action": {"buttons": [{"type": "reply", "reply": {"id": i, "title": t[:20]}} for i, t in buttons[:3]]}}
        if header:
            inter["header"] = {"type": "text", "text": header[:60]}
        if footer:
            inter["footer"] = {"text": footer[:60]}
        return await self.send_message(to, {"type": "interactive", "interactive": inter})

    async def send_interactive_list(self, to: str, body: str, button_label: str, rows: list[dict], section_title: str = "Opções", header: str | None = None,
                                    footer: str | None = None) -> str:
        """``rows``: [{"id","title","description"?}] — no máximo 10 linhas (limite da Meta)."""
        inter: dict[str, Any] = {"type": "list", "body": {"text": body[:1024]},
                                 "action": {"button": button_label[:20], "sections": [{"title": section_title[:24], "rows": [
                                     {"id": r["id"][:200], "title": r["title"][:24], **({"description": r["description"][:72]} if r.get("description") else {})} for r in rows[:10]]}]}}
        if header:
            inter["header"] = {"type": "text", "text": header[:60]}
        if footer:
            inter["footer"] = {"text": footer[:60]}
        return await self.send_message(to, {"type": "interactive", "interactive": inter})

    async def mark_read(self, wamid: str, typing: bool = False) -> None:
        if not self.live:
            return
        body: dict[str, Any] = {"messaging_product": "whatsapp", "status": "read", "message_id": wamid}
        if typing:
            body["typing_indicator"] = {"type": "text"}
        await self._request("POST", f"{self.phone_number_id}/messages", json=body)

    # ---------------------------------------------------------------- mídia
    async def get_media_url(self, media_id: str) -> dict:
        if not self.live:
            return {"url": f"sandbox://{media_id}", "mime_type": "application/octet-stream", "file_size": 0}
        return await self._request("GET", media_id)

    async def download_media(self, url: str) -> bytes:
        if not self.live or url.startswith("sandbox://"):
            return b"SANDBOX-MEDIA"
        return await self._request("GET", url, absolute=True, raw=True)

    async def upload_media(self, data: bytes, mime: str, filename: str) -> str:
        if not self.live:
            return self._fake_id("media")
        res = await self._request("POST", f"{self.phone_number_id}/media", data={"messaging_product": "whatsapp", "type": mime}, files={"file": (filename, data, mime)})
        return res["id"]

    # ---------------------------------------------------------------- templates
    async def create_template(self, name: str, language: str, category: str, components: list[dict]) -> dict:
        if not self.live:
            return {"id": self._fake_id("tpl"), "status": "PENDING", "category": category}
        return await self._request("POST", f"{self.waba_id}/message_templates", json={"name": name, "language": language, "category": category, "components": components})

    async def list_templates(self) -> list[dict]:
        if not self.live:
            return []
        out, url, params = [], f"{self.waba_id}/message_templates", {"limit": 100}
        res = await self._request("GET", url, params=params)
        out.extend(res.get("data", []))
        while res.get("paging", {}).get("next"):
            res = await self._request("GET", res["paging"]["next"], absolute=True)
            out.extend(res.get("data", []))
        return out

    async def delete_template(self, name: str) -> None:
        if self.live:
            await self._request("DELETE", f"{self.waba_id}/message_templates", params={"name": name})

    # ---------------------------------------------------------------- onboarding / contas
    async def exchange_code(self, code: str) -> dict:
        """Embedded Signup: troca o ``code`` do Facebook Login por um business token."""
        if not self.live:
            return {"access_token": f"SANDBOX_TOKEN_{uuid.uuid4().hex}", "expires_in": 5184000}
        return await self._request("GET", "oauth/access_token", params={"client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret, "code": code})

    async def exchange_long_lived(self, token: str) -> dict:
        if not self.live:
            return {"access_token": f"SANDBOX_TOKEN_{uuid.uuid4().hex}", "expires_in": 5184000}
        return await self._request("GET", "oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": settings.meta_app_id,
                                                                          "client_secret": settings.meta_app_secret, "fb_exchange_token": token})

    async def subscribe_app(self, waba_id: str, token: str | None = None) -> None:
        if self.live:
            await self._request("POST", f"{waba_id}/subscribed_apps", token=token)

    async def unsubscribe_app(self, waba_id: str, token: str | None = None) -> None:
        if self.live:
            await self._request("DELETE", f"{waba_id}/subscribed_apps", token=token)

    async def register_phone(self, phone_number_id: str, pin: str, token: str | None = None) -> None:
        if self.live:
            await self._request("POST", f"{phone_number_id}/register", json={"messaging_product": "whatsapp", "pin": pin}, token=token)

    async def get_phone_info(self, phone_number_id: str, token: str | None = None) -> dict:
        if not self.live:
            return {"display_phone_number": "+55 62 90000-0000", "verified_name": "Flowmentor (sandbox)", "quality_rating": "GREEN", "messaging_limit_tier": "TIER_1K"}
        return await self._request("GET", phone_number_id, params={"fields": "display_phone_number,verified_name,quality_rating,messaging_limit_tier"}, token=token)

    async def debug_token(self, token: str) -> dict:
        if not self.live:
            return {"data": {"is_valid": True, "expires_at": 0}}
        return await self._request("GET", "debug_token", params={"input_token": token, "access_token": f"{settings.meta_app_id}|{settings.meta_app_secret}"})
