"""Signal receivers for Nautobot DNS Models.

SOA serial maintenance on record deletion is handled with ``pre_delete`` / ``post_delete``
receivers rather than a ``DNSRecord.delete()`` override, because ``QuerySet.delete()`` (used by
the bulk-delete views and API) never calls ``Model.delete()``.

``Collector.delete()`` emits every ``pre_delete`` before any ``post_delete``. The receivers use
that ordering to collect all zones affected by a delete batch first, then increment them in
globally sorted (by primary key) order. Sorting is required for deadlock safety: two concurrent
bulk deletes touching the same zones in opposite record order would otherwise each hold one zone's
row lock and wait for the other. Per-transaction coalescing (each zone bumped at most once) is
handled by ``DNSZone._bump_zone_serial``.

These receivers operate on the default database; DNS records routed to a non-default database
are not supported.
"""

from constance import config as constance_config
from django.db import transaction
from django.db.models.signals import post_delete, pre_delete

from nautobot_dns_models.models import DNSRecord, DNSZone
from nautobot_dns_models.utils import get_transaction_scoped_state

# Instance attribute carrying the record's zone-id from pre_delete to post_delete; by the time
# post_delete runs the record row is gone, so the zone must be resolved beforehand. ``None`` means
# no increment was authorized — the feature was disabled at pre_delete, or the row was already
# deleted by another transaction. Used only as the autocommit fallback when there is no batch state.
_ZONE_ID_ATTR = "_soa_serial_zone_id"
# Connection attribute holding the per-delete-batch accumulator, keyed by savepoint scope.
_PENDING_DELETE_ATTR = "_soa_serial_pending_delete"


def _savepoint_scope():
    """Return a hashable key identifying the current transaction/savepoint nesting.

    ``connection.savepoint_ids`` snapshots the active atomic nesting. Sibling savepoints get
    distinct ids and a rolled-back savepoint's id is never reused, so keying accumulated zones by
    this tuple lets post_delete process only its own batch and ignore entries left behind by a
    rolled-back nested delete that shared the connection.

    Private-API dependency (like ``run_on_commit`` in ``utils.py``): revalidate this ``savepoint_ids``
    keying whenever the supported Django range changes; the delete-savepoint test suites cover it.
    """
    return tuple(transaction.get_connection().savepoint_ids)


def _pending_delete_zones():
    """Return the connection-bound ``{savepoint_scope: {zone_id, ...}}`` delete-batch accumulator.

    Transaction-scoped, so the outer transaction ending (commit or rollback) discards it before
    the connection serves the next request.
    """
    return get_transaction_scoped_state(_PENDING_DELETE_ATTR, dict)["payload"]


def capture_dns_record_zone(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Capture the record's authoritative zone before its row is deleted, and enqueue it.

    This is the single feature-flag decision point for the delete path. The attribute is cleared to
    ``None`` first, so a flag flip before ``post_delete`` cannot change the outcome and an instance
    reused after a failed delete cannot carry a stale captured zone into a later delete.

    When enabled, the zone is re-read under ``select_for_update()`` so a record moved by another
    transaction is counted against the zone it is actually deleted from. A captured zone is added to
    the current savepoint scope's batch so post_delete can bump all of the batch's zones together in
    sorted order. If the re-read finds no row, nothing is captured or enqueued.
    """
    setattr(instance, _ZONE_ID_ATTR, None)
    if not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT:
        return
    zone_id = sender.objects.select_for_update().values_list("zone_id", flat=True).filter(pk=instance.pk).first()
    setattr(instance, _ZONE_ID_ATTR, zone_id)
    if zone_id is not None:
        _pending_delete_zones().setdefault(_savepoint_scope(), set()).add(zone_id)


def increment_zone_serial_on_record_delete(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Bump each zone affected by this delete batch once, in globally sorted (by PK) order.

    ``pre_delete`` owns the feature-flag decision and enqueues zones per savepoint scope; the first
    ``post_delete`` of a batch drains its scope and bumps those zones sorted, which is deadlock-safe
    against concurrent bulk deletes touching the same zones in opposite order. Later ``post_delete``
    calls in the batch find the scope empty. Per-transaction coalescing is handled by
    ``_bump_zone_serial``. With no batch state (e.g. autocommit) fall back to this record's captured
    zone.
    """
    scopes = get_transaction_scoped_state(_PENDING_DELETE_ATTR, dict, create=False)
    if scopes is None:
        zone_id = getattr(instance, _ZONE_ID_ATTR, None)
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
        # _bump_zone_serial locks-or-skips in one query and coalesces per transaction: a missing
        # zone returns None rather than raising out of this receiver and aborting the delete.
        DNSZone._bump_zone_serial(zone_id)  # pylint: disable=protected-access


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
