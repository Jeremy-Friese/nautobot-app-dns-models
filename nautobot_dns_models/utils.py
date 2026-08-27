"""Utility functions for Nautobot DNS Models."""

from collections.abc import Iterable, Iterator

from django.db import transaction
from django.db.models import Count
from django.db.models.functions import Lower


def ancestor_keys(name: str) -> Iterator[str]:
    """Yield every name above the lowercase name `name`, most specific first.

    `a.example.com` yields `example.com`, then `com`.
    """
    labels = name.split(".")
    for index in range(1, len(labels)):
        yield ".".join(labels[index:])


def compute_zone_parents(zones: Iterable[tuple]) -> dict:
    """Map each zone's primary key to the primary key of its most specific enclosing zone, or `None`.

    Names compare case-insensitively (RFC 4343 §2), as zone name uniqueness does.

    Args:
        zones: `(pk, name, dns_view_id)` tuples. Zones only enclose zones in the same DNS view.
    """
    zone_list = list(zones)
    pks_by_view = {}
    for pk, name, dns_view_id in zone_list:
        pks_by_view.setdefault(dns_view_id, {}).setdefault(name.lower(), pk)

    parents = {}
    for pk, name, dns_view_id in zone_list:
        view_pks = pks_by_view[dns_view_id]
        parents[pk] = next((view_pks[key] for key in ancestor_keys(name.lower()) if key in view_pks), None)
    return parents


def get_dns_zone_name_collisions(dns_zone_model):
    """Return groups of DNS zones whose names differ only by letter case within the same DNS view.

    Args:
        dns_zone_model: The `DNSZone` model class.

    Returns:
        list[list]: One list of zones per collision, ordered by view name and then zone name.
    """
    zones = dns_zone_model.objects.annotate(name_lower=Lower("name"))
    collision_keys = (
        zones.values("dns_view", "name_lower")
        .order_by()
        .annotate(count=Count("pk"))
        .filter(count__gt=1)
        .values_list("dns_view", "name_lower")
    )
    collisions = []
    for dns_view, name_lower in collision_keys:
        collisions.append(
            list(zones.filter(dns_view=dns_view, name_lower=name_lower).select_related("dns_view").order_by("name"))
        )
    collisions.sort(key=lambda group: (group[0].dns_view.name, group[0].name.lower()))
    return collisions


def format_dns_zone_name_collisions(collisions, limit=None, record_counts=None):
    """Return a human-readable description of DNS zone name collisions.

    Args:
        collisions (list[list]): Groups of colliding zones, as returned by `get_dns_zone_name_collisions`.
        limit (int, optional): Maximum number of groups to describe; the remainder is summarized in one line.
        record_counts (dict, optional): Mapping of zone primary key to the number of records in the zone.

    Returns:
        str: The description, one line per group of colliding zones.
    """
    lines = [
        f"Found {len(collisions)} group(s) of DNS zones whose names differ only by letter case within the same DNS view.",
    ]
    for group in collisions if limit is None else collisions[:limit]:
        zone_descriptions = []
        for zone in group:
            description = f'"{zone.name}" (ID {zone.pk}'
            if record_counts is not None:
                description += f", {record_counts.get(zone.pk, 0)} record(s)"
            zone_descriptions.append(f"{description})")
        lines.append(f'    View "{group[0].dns_view.name}": {", ".join(zone_descriptions)}')
    if limit is not None and len(collisions) > limit:
        lines.append(f"    ...and {len(collisions) - limit} more.")
    return "\n".join(lines)


def _discard_state(conn, attr_name):
    """Remove connection-bound state, tolerating it already being gone."""
    try:
        delattr(conn, attr_name)
    except AttributeError:
        pass


def _callback_is_pending(conn, hook):
    """Return True if ``hook`` is still registered in ``conn``'s pending on_commit callbacks.

    ``connection.run_on_commit`` is a Django internal: tuples whose 2nd element is the callback --
    ``(sids, func)`` through Django 4.1, ``(sids, func, robust)`` from 4.2 on, and both 4.2 and 5.2
    are in play for this app.  An unrecognised shape degrades to "assume stale" rather than
    raising IndexError.

    Private-API dependency: revalidate this parsing (and the ``savepoint_ids`` keying in
    ``signals.py``) whenever the supported Django range changes; the ``SOASerialLiveness`` and
    savepoint test suites exercise it.
    """
    for entry in getattr(conn, "run_on_commit", ()):
        try:
            if entry[1] is hook:
                return True
        except (IndexError, TypeError):  # pragma: no cover - future Django callback shape
            if callable(entry) and entry is hook:
                return True
    return False


def register_transaction_marker():
    """Register and return a new no-op ``on_commit`` callback that marks the current savepoint nesting.

    ``on_commit`` records the savepoints active at registration with each callback, and a savepoint
    rollback drops every callback registered inside it, at any depth. A savepoint release keeps
    them. So the marker stays pending until the transaction ends, unless a savepoint that enclosed
    the registration is rolled back. Check it with ``transaction_marker_is_pending``.

    Each call creates a new function object: a pending check compares by identity, so a shared
    callable would read as pending after its own registration was rolled back.

    Returns:
        callable: the registered marker, or None when ``on_commit`` refuses to register (manual
        transaction management). None is never pending.
    """

    def _marker():
        """Do nothing on commit; the marker only matters while it is pending."""

    try:
        transaction.on_commit(_marker)
    except transaction.TransactionManagementError:
        return None
    return _marker


def transaction_marker_is_pending(marker):
    """Return True if ``marker`` from ``register_transaction_marker`` is still pending.

    A pending marker means the transaction that registered it is still open and no savepoint that
    enclosed the registration has been rolled back. None, or a callback shape this app does not
    recognize, reads as not pending.
    """
    if marker is None:
        return False
    return _callback_is_pending(transaction.get_connection(), marker)


def get_transaction_scoped_state(attr_name, payload_factory, create=True):
    """Return connection-bound state that cannot outlive the transaction that created it.

    State is attached to the database connection (not thread-local) and guarded by an
    ``on_commit`` hook.  Once the outer atomic block ends the hook is gone from
    ``conn.run_on_commit`` -- called and drained on commit, dropped on rollback -- so an absent
    hook means the state belongs to a finished transaction and must not be reused.  Without
    that check a transaction failing partway through would leave state behind for the next
    request or worker task on the same connection to consume.

    Args:
        attr_name (str): connection attribute the state is stored under.
        payload_factory (callable): builds the payload when new state is created.
        create (bool): when False, return None instead of creating state.

    Returns:
        dict: the ``{"payload": ..., "hook": ...}`` wrapper, or None when no live state exists
        and ``create`` is False.
    """
    conn = transaction.get_connection()
    state = getattr(conn, attr_name, None)

    if state is not None and not _callback_is_pending(conn, state["hook"]):
        _discard_state(conn, attr_name)
        state = None

    if state is None and create:

        def _clear():
            current = getattr(conn, attr_name, None)
            if current is not None and current["hook"] is _clear:
                _discard_state(conn, attr_name)

        try:
            transaction.on_commit(_clear)
        except transaction.TransactionManagementError:
            # Reached only under manual transaction management (autocommit disabled with no atomic
            # block), where on_commit() refuses to register. In plain autocommit on_commit() instead
            # runs _clear immediately. Either way no live hook remains, so the next caller finds
            # nothing pending and treats this state as stale -- correct, because without an enclosing
            # transaction there is no cross-statement state worth carrying over.
            pass

        setattr(conn, attr_name, {"payload": payload_factory(), "hook": _clear})
        state = getattr(conn, attr_name)

    return state
