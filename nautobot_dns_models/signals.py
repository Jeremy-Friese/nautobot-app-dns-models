"""Signal receivers for Nautobot DNS Models.

SOA serial maintenance on record deletion is handled here rather than in a
``DNSRecord.delete()`` override. ``QuerySet.delete()`` — which the Nautobot bulk-delete
views use — never calls ``Model.delete()``, so an override silently misses every bulk
deletion performed through the UI or API. ``pre_delete`` / ``post_delete`` fire on both
paths, so a single receiver pair covers them.

Registering these receivers also disables Django's "fast delete" optimization for the
record models: the collector must fetch each row and emit signals instead of issuing a
single bulk ``DELETE``. That cost is what buys correct serial tracking on bulk deletes.

``Collector.delete()`` sends ``pre_delete`` for every instance before any ``post_delete``.
The receivers use that ordering to collect all affected zones first, then increment them
in globally sorted order to avoid opposing bulk-delete transactions deadlocking on zone
row locks.
"""

from constance import config as constance_config
from django.db import transaction
from django.db.models.signals import post_delete, pre_delete

from nautobot_dns_models.models import DNSRecord, DNSZone
from nautobot_dns_models.utils import get_transaction_scoped_state

# Attribute used to carry the locked zone id from pre_delete to post_delete. By the time
# post_delete runs the row is gone, so the zone must be resolved while it still exists.
_ZONE_ID_ATTR = "_soa_serial_zone_id"
_PENDING_ZONE_IDS_ATTR = "_soa_serial_pending_delete_zone_ids"


def _get_pending_delete_state(create=True):
    """Return the transaction-scoped state wrapper for the current delete batch.

    Transaction-scoped, so a delete that fails after ``pre_delete`` has run cannot leave
    captured zone IDs behind for the next request or worker task to consume.
    """
    return get_transaction_scoped_state(_PENDING_ZONE_IDS_ATTR, set, create=create)


def _get_pending_delete_zone_ids():
    """Return the zone-id set captured during the current delete batch."""
    return _get_pending_delete_state()["payload"]


def _pop_pending_delete_zone_ids(instance):
    """Return pending delete zone IDs once per Collector.delete() post_delete batch."""
    state = _get_pending_delete_state(create=False)
    if state is None:
        zone_id = getattr(instance, _ZONE_ID_ATTR, None)
        if zone_id is None:
            zone_id = instance.zone_id
        return [] if zone_id is None else [zone_id]

    zone_ids = state["payload"]
    pending = sorted(zone_ids, key=str)
    zone_ids.clear()
    return pending


def capture_dns_record_zone(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Record the deleted record's authoritative zone before the row disappears.

    The zone is re-read under ``select_for_update()`` rather than trusting
    ``instance.zone_id``, which may be stale if another transaction moved the record.
    This preserves the guarantee the previous ``delete()`` override provided.
    """
    if not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT:
        return

    with transaction.atomic():
        zone_id = sender.objects.select_for_update().values_list("zone_id", flat=True).filter(pk=instance.pk).first()

    # Fall back to the in-memory value if the row is already gone (re-entrant delete).
    zone_id = zone_id if zone_id is not None else instance.zone_id
    setattr(instance, _ZONE_ID_ATTR, zone_id)
    if zone_id is not None:
        _get_pending_delete_zone_ids().add(zone_id)


def increment_zone_serial_on_record_delete(sender, instance, **kwargs):  # pylint: disable=unused-argument
    """Increment the parent zone's SOA serial after a record is deleted.

    Multiple deletions inside one transaction coalesce to a single increment per zone,
    the same as record creates and updates.
    """
    if not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT:
        return

    for zone_id in _pop_pending_delete_zone_ids(instance):
        # filter().first() rather than get(): a missing zone must not raise out of a signal
        # receiver and abort the surrounding delete.
        zone = DNSZone.objects.filter(pk=zone_id).first()
        if zone is not None:
            zone.increment_soa_serial()


def connect_dns_record_signals():
    """Connect the SOA serial receivers to every concrete DNSRecord subclass.

    ``DNSRecord`` is abstract, so signals cannot be connected to it directly and each
    concrete record model must be wired individually. Connecting per model — rather than
    with a sender-less receiver — keeps these handlers off every other model in Nautobot.
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
