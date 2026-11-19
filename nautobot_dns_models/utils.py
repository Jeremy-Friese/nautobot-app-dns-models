"""Utility functions for Nautobot DNS Models."""

from django.db import transaction


def _discard_state(conn, attr_name):
    """Remove connection-bound state, tolerating it already being gone."""
    try:
        delattr(conn, attr_name)
    except AttributeError:
        pass


def _callback_is_pending(conn, hook):
    """Return True if ``hook`` is still among ``conn``'s pending on_commit callbacks.

    ``connection.run_on_commit`` is a Django internal holding ``(savepoint_ids, func, robust)`` tuples; an
    entry of any other shape reads as not pending. Recheck this, and the ``savepoint_ids`` keying in
    ``signals.py``, when the supported Django range changes.
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
