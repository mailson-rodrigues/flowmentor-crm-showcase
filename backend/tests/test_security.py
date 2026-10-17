import base64
import os

from app.config import settings
from app.modules.ops.pix import brcode, crc16
from app.modules.whatsapp.templates import validate_template
from app.security import crypto
from app.security.masking import mask_email, mask_phone, mask_tax_id, scrub_text
from app.security.passwords import validate_password_policy
from app.security.rbac import has_permission
from app.security.sanitize import sanitize_text


def test_envelope_encryption_roundtrip_and_rotation(monkeypatch):
    token = crypto.encrypt("12345678000190")
    assert token.startswith("enc:v1:") and "12345678000190" not in token
    assert crypto.decrypt(token) == "12345678000190"
    assert crypto.encrypt("12345678000190") != token  # DEK/nonce aleatórios a cada valor
    old = crypto.provider()
    k2 = base64.urlsafe_b64encode(os.urandom(32)).decode()
    dev = base64.urlsafe_b64encode(old.keys["dev"]).decode()
    monkeypatch.setattr(settings, "kek_keys", f"k2:{k2},dev:{dev}")
    crypto.reset_provider()
    try:
        assert crypto.decrypt(token) == "12345678000190"  # chave antiga ainda decifra
        new = crypto.rewrap(token)
        assert ":k2:" in new and crypto.decrypt(new) == "12345678000190"
    finally:
        monkeypatch.undo()
        crypto.reset_provider()


def test_blind_index_is_deterministic():
    assert crypto.blind_index("123", "tax_id") == crypto.blind_index("123", "tax_id")
    assert crypto.blind_index("123", "tax_id") != crypto.blind_index("124", "tax_id")


def test_masking():
    assert mask_phone("5562999999989") == "+55 (62) 9****-**89"
    assert mask_tax_id("12.345.678/0001-90") == "**.***.678/****-90"
    assert mask_email("maria@exemplo.com") == "m****@exemplo.com"
    assert "[CNPJ]" in scrub_text("CNPJ 12.345.678/0001-90") and "[EMAIL]" in scrub_text("a@b.com")


def test_sanitize_xss():
    assert "<script" not in sanitize_text("oi<script>alert(1)</script> tudo bem")
    assert sanitize_text("<img src=x onerror=alert(1)>olá") == "olá"
    assert "javascript:" not in sanitize_text("clique javascript:alert(1)")


def test_password_policy():
    assert validate_password_policy("Abc#12345678") == []
    assert validate_password_policy("fraca")


def test_rbac_matrix():
    assert has_permission("admin", "audit:view")
    assert not has_permission("agent", "pii:view") and has_permission("agent", "pii:reveal")
    assert has_permission("supervisor", "supervisor:live") and not has_permission("agent", "supervisor:live")
    assert has_permission("auditor", "lgpd:manage") and not has_permission("auditor", "deals:write")


def test_pix_brcode_crc():
    code = brcode("pix@flowmentor.com.br", "Flowmentor", "Goiania", 1500, "FAT20260001")
    assert code.endswith(crc16(code[:-4]))
    assert "br.gov.bcb.pix" in code and "1500.00" in code


def test_template_validation_rules():
    ok = validate_template("laudo_pronto", "pt_BR", "UTILITY", [
        {"type": "BODY", "text": "Olá {{1}}, o laudo do exame {{2}} está disponível para consulta na plataforma."},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Obrigado"}]}], {"1": "Maria", "2": "A123"})
    assert ok["errors"] == []
    bad = validate_template("Nome Inválido", "pt_BR", "UTILITY", [{"type": "BODY", "text": "{{1}} começa com variável {{3}}"}])
    assert any("minúsculas" in e for e in bad["errors"])
    assert any("começar" in e for e in bad["errors"])
    assert any("sequenciais" in e for e in bad["errors"])
    btns = validate_template("x_y", "pt_BR", "UTILITY", [
        {"type": "BODY", "text": "Texto de teste simples aqui"},
        {"type": "BUTTONS", "buttons": [{"type": "URL", "text": "A", "url": "http://x.com"}, {"type": "URL", "text": "B", "url": "https://y.com"},
                                         {"type": "URL", "text": "C", "url": "https://z.com"}]}])
    assert any("2 botões de URL" in e for e in btns["errors"])
    assert any("https" in e for e in btns["errors"])
