"""Signal receivers for Nautobot DNS Models.

SOA serial maintenance on record deletion is handled with ``pre_delete`` / ``post_delete``
receivers rather than a ``DNSRecord.delete()`` override. ``QuerySet.delete()`` — used by
Nautobot's bulk-delete views and API — never calls ``Model.delete()``, so an override would
silently miss every bulk deletion. Connecting these receivers also disables Django's
"fast delete" optimization for the record models, so the collector fetches each row and
emits signals; that per-row cost is what makes bulk-delete serial tracking correct.

``Collector.delete()`` emits every ``pre_delete`` before any ``post_delete``. The receivers
use that ordering to collect all zones affected by a delete batch first, then increment them
in globally sorted (by primary key) order. Sorting is required: two concurrent bulk deletes
touching the same zones in opposite record order would otherwise each hold one zone's row
lock and wait for the other, deadlocking.
"""

from constance import config as constance_config
from django.db import transaction
from django.db.models.signals import post_delete, pre_delete

from nautobot_dns_models.models import DNSRecord, DNSZone
from nautobot_dns_models.utils import get_transaction_scoped_state

# Instance attribute carrying the locked zone-id from pre_delete to post_delete; by the time
# post_delete runs the record row is gone, so the zone must be resolved beforehand.
_ZONE_ID_ATTR = "_soa_serial_zone_id"
# Connection attribute holding the per-delete-batch accumulator (see _pending_delete_zones).
_PENDING_DELETE_ATTR = "_soa_serial_pending_delete"


def _savepoint_scope():
    """Return a hashable key identifying the current transaction/savepoint scope.

    ``connection.savepoint_ids`` snapshots the active atomic nesting. Sibling savepoints get
    distinct ids and a rolled-back savepoint's id is never reused, so keying accumulated zones
    by this tuple lets post_delete process only its own batch and ignore entries left behind by
    a rolled-back nested delete that shared the connection.
    """
    return tuple(transaction.get_connection().savepoint_ids)


def _pending_delete_zones():
    """Return the connection-bound ``{savepoint_scope: {zone_id, ...}}`` accumulator.

    Transaction-scoped, so the outer transaction ending (commit or rollback) discards it before
    the connection serves the next request.
    """
    return get_transaction_scoped_state(_PENDING_DELETE_ATTR, dict)["payload"]


def capture_dns_record_zone(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Record the deleted record's authoritative zone before its row disappears.

    The zone is re-read under ``select_for_update()`` — rather than trusting a possibly stale
    ``instance.zone_id`` — so a concurrent move cannot change the record's zone between here and
    the delete. This runs inside ``Collector.delete()``'s transaction, which the lock is scoped
    to.
    """
    if not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT:
        return

    zone_id = sender.objects.select_for_update().values_list("zone_id", flat=True).filter(pk=instance.pk).first()
    # Fall back to the in-memory value if the row is already gone (re-entrant delete).
    zone_id = zone_id if zone_id is not None else instance.zone_id
    setattr(instance, _ZONE_ID_ATTR, zone_id)
    if zone_id is not None:
        _pending_delete_zones().setdefault(_savepoint_scope(), set()).add(zone_id)


def increment_zone_serial_on_record_delete(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Increment each affected zone's SOA serial once per delete batch, in sorted order.

    The first ``post_delete`` of a batch drains the zones captured for this savepoint scope and
    increments them in primary-key order; later ``post_delete`` calls in the same batch find the
    scope empty. Sorted acquisition keeps concurrent bulk deletes from deadlocking on zone locks.
    Coalescing across the wider transaction is handled by ``increment_soa_serial`` itself.
    """
    if not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT:
        return

    scopes = get_transaction_scoped_state(_PENDING_DELETE_ATTR, dict, create=False)
    if scopes is None:
        # No batch state (e.g. auto-commit): fall back to this record's captured zone.
        zone_id = getattr(instance, _ZONE_ID_ATTR, None) or instance.zone_id
        zone_ids = [zone_id] if zone_id is not None else []
    else:
        payload = scopes["payload"]
        current = _savepoint_scope()
        zone_ids = sorted(payload.pop(current, ()), key=str)
        # Drop scopes that are no longer part of the live atomic nesting (rolled-back savepoints).
        live_scopes = {current[:i] for i in range(len(current) + 1)}
        for scope in [s for s in payload if s not in live_scopes]:
            del payload[scope]

    for zone_id in zone_ids:
        # filter().first() rather than get(): a missing zone must not raise out of a signal
        # receiver and abort the surrounding delete.
        zone = DNSZone.objects.filter(pk=zone_id).first()
        if zone is not None:
            zone.increment_soa_serial()


def connect_dns_record_signals():
    """Connect the SOA serial receivers to every concrete DNSRecord subclass.

    ``DNSRecord`` is abstract, so each concrete record model is wired individually. Connecting
    per model — rather than with a sender-less receiver — keeps these handlers off every other
    model in Nautobot.
    """
    for model in DNSRecord.__subclasses__():
        if model._meta.abstract:  # pylint: disable=protected-access
            continue
        pre_delete.connect(
            capture_dns_record_zone,
            sender=model,
            dispatch_uid=f"nautobot_dns_models.capture_zone.{model._meta.label_lower}",  # pylint: disable=protected-access
        )
        post_delete.connect(
            increment_zone_serial_on_record_delete,
            sender=model,
            dispatch_uid=f"nautobot_dns_models.increment_serial.{model._meta.label_lower}",  # pylint: disable=protected-access
        )
