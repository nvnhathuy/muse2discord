"""Integration layer between the hub core (bridgehub) and the admin
package (hub_admin): one shared agent registry with hot reload, a
routing gate, bot lifecycle management, and a live status snapshot.

Everything here is additive: the hub core keeps its own modules, this
package supplies the seams described in PATCH-POINTS.md.
"""

__all__ = ["registry_sync", "routing_gate", "bot_lifecycle", "status_snapshot"]
