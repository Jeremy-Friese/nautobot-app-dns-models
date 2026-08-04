"""Shared helpers for Nautobot DNS Models."""

from django.db import transaction


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
    """
    for entry in getattr(conn, "run_on_commit", ()):
        try:
            if entry[1] is hook:
                return True
        except (IndexError, TypeError):  # pragma: no cover - future Django callback shape
            if callable(entry) and entry is hook:
                return True
    return False


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
            # No outer atomic to register against.  In autocommit each statement stands alone,
            # so there is no cross-statement state worth keeping, and the next caller will find
            # no pending hook and treat this state as stale.
            pass

        # attr_name is a parameter, so setattr is required here.
        setattr(conn, attr_name, {"payload": payload_factory(), "hook": _clear})
        state = getattr(conn, attr_name)

    return state
