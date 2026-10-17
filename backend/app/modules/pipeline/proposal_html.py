"""Renderização da proposta contratual em HTML imprimível (CSS externo por causa da CSP estrita)."""
from __future__ import annotations

from jinja2 import Environment, select_autoescape

_env = Environment(autoescape=select_autoescape(default=True, default_for_string=True))

TEMPLATE = _env.from_string("""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proposta {{ p.number }}</title><link rel="stylesheet" href="/static/css/proposal.css"></head>
<body><main class="sheet">
<header class="head"><div><h1>{{ company }}</h1><p class="tag">{{ tagline }}</p></div>
<div class="meta"><strong>Proposta {{ p.number }}</strong><span>Versão {{ p.version }}</span>
<span>Validade: {{ p.valid_until.strftime('%d/%m/%Y') if p.valid_until else '-' }}</span></div></header>
<section><h2>Cliente</h2><p><strong>{{ snap.account.name }}</strong>{% if snap.account.trade_name %} ({{ snap.account.trade_name }}){% endif %}</p></section>
{% if p.scope_text %}<section><h2>Escopo</h2><p class="pre">{{ p.scope_text }}</p></section>{% endif %}
<section><h2>Itens</h2>
<table><thead><tr><th>Descrição</th><th>Categoria</th><th class="n">Qtd</th><th class="n">Valor unit.</th><th class="n">Subtotal</th></tr></thead><tbody>
{% for i in snap['items'] %}<tr><td>{{ i.description }}<small>{{ i.sku }}</small></td><td>{{ i.category }}</td>
<td class="n">{{ '%.2f'|format(i.quantity) }} {{ i.unit or '' }}</td><td class="n">{{ money(i.unit_price) }}</td><td class="n">{{ money(i.subtotal) }}</td></tr>{% endfor %}
</tbody><tfoot>
<tr><td colspan="4" class="n">Subtotal</td><td class="n">{{ money(snap.subtotal) }}</td></tr>
{% if snap.discount_percentage %}<tr><td colspan="4" class="n">Desconto institucional ({{ '%.2f'|format(snap.discount_percentage) }}%)</td><td class="n">- {{ money(snap.subtotal - snap.total) }}</td></tr>{% endif %}
<tr class="total"><td colspan="4" class="n">Total</td><td class="n">{{ money(snap.total) }}</td></tr></tfoot></table></section>
<section><h2>Condições de pagamento</h2><p class="pre">{{ p.payment_terms or '-' }}</p></section>
<section><h2>Cláusulas</h2>{% for c in p.clauses %}<h3>{{ loop.index }}. {{ c.title }}</h3><p>{{ c.text }}</p>{% endfor %}</section>
<footer><div class="sign"><span>{{ company }}</span></div><div class="sign"><span>{{ snap.account.name }}</span></div></footer>
<p class="noprint"><button id="printBtn" type="button">Imprimir / Salvar PDF</button></p>
</main><script src="/static/js/print.js"></script></body></html>""")


def _money(v: float) -> str:
    s = f"{v:,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def render_proposal(proposal, org) -> str:
    branding = (org.settings or {}).get("branding", {})
    return TEMPLATE.render(p=proposal, snap=proposal.snapshot, money=_money, company=branding.get("company", org.name),
                           tagline=branding.get("tagline", "Inteligência em Citometria de Fluxo"))
