"""Central registry: subscribes the consumers to the bus and loads periodic tasks/jobs."""
from __future__ import annotations

from app.core.events import EventBus


def register_all(bus: EventBus) -> None:
    from app.core import partitions  # noqa: F401
    from app.modules.ops import handlers as ops_handlers
    from app.modules.webhooks import router as webhooks_router
    from app.modules.whatsapp import accounts_router, bridge, kpis, outbound, processor, realtime, routing, supervisor, surveys  # noqa: F401  (decoradores @periodic/@job)

    bus.subscribe("whatsapp.webhook.received", processor.handle_webhook, "whatsapp.processor")
    bus.subscribe("message.outbound_queued", outbound.send_outbound, "whatsapp.outbound")
    for evt in ("agent.status_changed", "ticket.closed", "ticket.resolved", "ticket.queued", "ticket.transferred"):
        bus.subscribe(evt, routing.on_capacity_event, f"routing.{evt}")
    bus.subscribe("ticket.closed", surveys.on_ticket_closed, "surveys.on_ticket_closed")
    bridge.register(bus)
    ops_handlers.register(bus)
    webhooks_router.register(bus)
    realtime.register(bus)
