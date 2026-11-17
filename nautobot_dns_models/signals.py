"""Signal handlers for nautobot_dns_models.

SOA serial maintenance on record deletion is handled with ``pre_delete`` / ``post_delete``
receivers rather than a ``DNSRecord.delete()`` override, because ``QuerySet.delete()`` (used by
the bulk-delete views and API) never calls ``Model.delete()``.

``Collector.delete()`` emits every ``pre_delete`` before any ``post_delete``. The receivers use
that ordering to collect all zones affected by a delete batch first, then increment them in
globally sorted (by primary key) order. Sorting is required for deadlock safety: two concurrent
bulk deletes touching the same zones in opposite record order would otherwise each hold one zone's
row lock and wait for the other. Per-transaction coalescing (each zone bumped at most once) is
handled by ``DNSZone.bump_zone_serial``.

These receivers operate on the default database; DNS records routed to a non-default database
are not supported.
"""

from constance import config as constance_config
from django.db import transaction
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from nautobot_dns_models.models import DNSRecord, DNSZone, rebuild_zone_hierarchy
from nautobot_dns_models.utils import get_transaction_scoped_state

# Connection attribute holding the per-delete-batch accumulator, keyed by savepoint scope.
_PENDING_DELETE_ATTR = "_soa_serial_pending_delete"


@receiver(post_delete, sender=DNSZone)
def reparent_zones_after_delete(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Recompute the parent of every zone in the deleted zone's DNS View.

    `on_delete=SET_NULL` clears the parent of the deleted zone's children first. A signal rather than
    `DNSZone.delete()` also covers `QuerySet.delete()`, which Nautobot's bulk delete uses.
    """
    rebuild_zone_hierarchy(dns_view=instance.dns_view_id)


def _savepoint_scope():
    """Return a hashable key for the current transaction and savepoint nesting.

    Sibling savepoints get distinct ids and a rolled-back savepoint's id is never reused, so a delete batch
    keyed by this tuple is never mixed with one left behind by a rolled-back nested delete.
    ``connection.savepoint_ids`` is a Django internal: recheck this when the supported Django range changes.
    """
    return tuple(transaction.get_connection().savepoint_ids)


def _pending_delete_zones():
    """Return the transaction-scoped ``{savepoint_scope: {zone_id, ...}}`` map of zones awaiting a delete bump."""
    return get_transaction_scoped_state(_PENDING_DELETE_ATTR, dict)["payload"]


def capture_dns_record_zone(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Queue the zone of a record that is about to be deleted for a serial bump.

    The feature flag is read here only, so a flag change before ``post_delete`` does not alter the outcome.
    The zone is read under a row lock, so a record moved by another transaction counts against the zone it
    is deleted from.
    """
    if not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT:
        return

    zone_id = sender.objects.select_for_update().values_list("zone_id", flat=True).filter(pk=instance.pk).first()
    if zone_id is not None:
        _pending_delete_zones().setdefault(_savepoint_scope(), set()).add(zone_id)


def increment_zone_serial_on_record_delete(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Bump every zone queued by this delete batch once, in primary key order."""
    scopes = get_transaction_scoped_state(_PENDING_DELETE_ATTR, dict, create=False)
    if scopes is None:
        return

    payload = scopes["payload"]
    current = _savepoint_scope()
    zone_ids = sorted(payload.pop(current, ()), key=str)
    # Forget batches queued inside savepoints that have since been rolled back.
    live_scopes = {current[:i] for i in range(len(current) + 1)}
    for scope in [scope for scope in payload if scope not in live_scopes]:
        del payload[scope]

    for zone_id in zone_ids:
        DNSZone.bump_zone_serial(zone_id)


def connect_dns_record_signals():
    """Connect the SOA serial receivers to every concrete DNSRecord subclass."""
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
