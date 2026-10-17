"""Gateways de pagamento (porta) e gerador de BR Code PIX estático ("copia e cola") com CRC16."""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal


def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def _ascii(s: str, n: int) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9 .\-]", "", s)[:n].strip().upper()


def crc16(payload: str) -> str:
    crc = 0xFFFF
    for ch in payload.encode():
        crc ^= ch << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def brcode(pix_key: str, merchant_name: str, merchant_city: str, amount: Decimal | None = None, txid: str = "***") -> str:
    """Gera o payload EMV do PIX (BR Code estático), válido para leitura em qualquer app bancário."""
    account = _tlv("00", "br.gov.bcb.pix") + _tlv("01", pix_key)
    payload = (
        _tlv("00", "01") + _tlv("26", account) + _tlv("52", "0000") + _tlv("53", "986")
        + (_tlv("54", f"{Decimal(amount):.2f}") if amount else "")
        + _tlv("58", "BR") + _tlv("59", _ascii(merchant_name, 25) or "FLOWMENTOR")
        + _tlv("60", _ascii(merchant_city, 15) or "GOIANIA")
        + _tlv("62", _tlv("05", re.sub(r"[^A-Za-z0-9]", "", txid)[:25] or "***"))
        + "6304"
    )
    return payload + crc16(payload)


class PixGateway:
    """Porta para gateways (Asaas, Efí, Mercado Pago...). A implementação padrão gera BR Code estático."""

    async def create_charge(self, *, invoice_number: str, amount: Decimal, settings: dict) -> dict:
        key = (settings.get("billing") or {}).get("pix_key")
        if not key:
            return {"payload": None, "gateway_ref": None, "error": "Chave PIX não configurada em Configurações > Faturamento"}
        b = settings["billing"]
        return {"payload": brcode(key, b.get("merchant_name", "Flowmentor"), b.get("merchant_city", "Goiania"), amount, invoice_number),
                "gateway_ref": f"static:{invoice_number}"}


class NfseProvider:
    """Porta de emissão de NFS-e (integração com prefeitura/ERP). Padrão: marca como pendente e emite evento."""

    async def request(self, invoice) -> dict:
        return {"status": "pending", "number": None}
