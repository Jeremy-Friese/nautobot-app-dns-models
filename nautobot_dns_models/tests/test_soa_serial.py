"""Tests for SOA serial auto-increment."""

# These tests deliberately inspect DNSZone._initial_soa_serial, the private snapshot that
# clean() validates against; there is no public accessor for it by design.
# pylint: disable=protected-access,too-many-lines

import threading
from unittest import skipUnless

from constance.test import override_config
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models.signals import pre_delete
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from nautobot.apps.testing import APIViewTestCases, TestCase, TransactionTestCase
from nautobot.extras.models import Status
from nautobot.ipam.models import IPAddress, Namespace, Prefix
from rest_framework import status as http_status

from nautobot_dns_models.forms import DNSZoneForm, TXTRecordForm
from nautobot_dns_models.models import (
    UINT32_MAX,
    AAAARecord,
    ARecord,
    CNAMERecord,
    DNSZone,
    MXRecord,
    NSRecord,
    PTRRecord,
    SRVRecord,
    TXTRecord,
)
from nautobot_dns_models.signals import _get_pending_delete_state


def _reset_dirty_zones_for_testing():
    """Clear serial dedup state and assert delete-batch state has not leaked."""
    conn = connection
    try:
        delattr(conn, "_dns_dirty_zones")
    except AttributeError:
        pass

    pending_state = _get_pending_delete_state(create=False)
    if pending_state is not None and pending_state["payload"]:
        raise AssertionError(f"pending delete zone IDs leaked across test boundary: {pending_state['payload']}")


def _refresh_serial(zone):
    """Reload zone from DB and return soa_serial."""
    zone.refresh_from_db()
    return zone.soa_serial


# ── shared fixture helpers ─────────────────────────────────────────────────────


def _create_zone(name, serial=0):
    """Create a DNSZone with the given name and starting serial."""
    return DNSZone.objects.create(
        name=name,
        filename=f"{name}.zone",
        soa_mname=f"ns1.{name}",
        soa_rname=f"admin@{name}",
        soa_serial=serial,
    )


# ── per-record-type create tests ───────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialCreateTestCase(TestCase):
    """Test that creating any record type triggers one serial increment."""

    @classmethod
    def setUpTestData(cls):
        """Create shared zone and IP fixtures."""
        cls.zone = _create_zone("create-test.example")
        status = Status.objects.get(name="Active")
        namespace = Namespace.objects.get(name="Global")
        Prefix.objects.create(prefix="10.51.0.0/24", namespace=namespace, type="Pool", status=status)
        Prefix.objects.create(prefix="2001:db8:c1::/64", namespace=namespace, type="Pool", status=status)
        cls.ipv4 = IPAddress.objects.create(address="10.51.0.1/32", namespace=namespace, status=status)
        cls.ipv6 = IPAddress.objects.create(address="2001:db8:c1::1/128", namespace=namespace, status=status)

    def setUp(self):
        """Reset zone serial and dedup state before each test."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=0)
        self.zone.refresh_from_db()
        _reset_dirty_zones_for_testing()

    def test_nsrecord_create_increments_serial(self):
        NSRecord.objects.create(name="ns-c", server="ns.create-test.example.", zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 1)

    def test_arecord_create_increments_serial(self):
        ARecord.objects.create(name="a-c", ip_address=self.ipv4, zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 1)

    def test_aaaarecord_create_increments_serial(self):
        AAAARecord.objects.create(name="aaaa-c", ip_address=self.ipv6, zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 1)

    def test_cnamerecord_create_increments_serial(self):
        CNAMERecord.objects.create(name="cname-c", alias="target.example.", zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 1)

    def test_mxrecord_create_increments_serial(self):
        MXRecord.objects.create(name="mx-c", mail_server="mail.example.", zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 1)

    def test_txtrecord_create_increments_serial(self):
        TXTRecord.objects.create(name="txt-c", text="v=spf1 -all", zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 1)

    def test_ptrrecord_create_increments_serial(self):
        PTRRecord.objects.create(name="ptr-c", ptrdname="host.example.", zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 1)

    def test_srvrecord_create_increments_serial(self):
        SRVRecord.objects.create(
            name="_sip._tcp.c", priority=10, weight=5, port=5060, target="sip.example.", zone=self.zone
        )
        self.assertEqual(_refresh_serial(self.zone), 1)


# ── per-record-type update tests ───────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialUpdateTestCase(TestCase):
    """Test that updating any record type triggers a serial increment."""

    @classmethod
    def setUpTestData(cls):
        """Create shared zone and IP fixtures."""
        cls.zone = _create_zone("update-test.example")
        status = Status.objects.get(name="Active")
        namespace = Namespace.objects.get(name="Global")
        Prefix.objects.create(prefix="10.52.0.0/24", namespace=namespace, type="Pool", status=status)
        Prefix.objects.create(prefix="2001:db8:c2::/64", namespace=namespace, type="Pool", status=status)
        cls.ipv4_a = IPAddress.objects.create(address="10.52.0.1/32", namespace=namespace, status=status)
        cls.ipv4_b = IPAddress.objects.create(address="10.52.0.2/32", namespace=namespace, status=status)
        cls.ipv6_a = IPAddress.objects.create(address="2001:db8:c2::1/128", namespace=namespace, status=status)
        cls.ipv6_b = IPAddress.objects.create(address="2001:db8:c2::2/128", namespace=namespace, status=status)

    def setUp(self):
        """Reset zone serial and dedup state before each test."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=0)
        self.zone.refresh_from_db()
        _reset_dirty_zones_for_testing()

    def _serial_after_create_and_reset(self):
        """Return serial after create; reset dedup state for the update assertion."""
        serial = _refresh_serial(self.zone)
        _reset_dirty_zones_for_testing()
        return serial

    def test_nsrecord_update_increments_serial(self):
        rec = NSRecord.objects.create(name="ns-u", server="ns1.update-test.example.", zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.server = "ns2.update-test.example."
        rec.save()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_arecord_update_increments_serial(self):
        rec = ARecord.objects.create(name="a-u", ip_address=self.ipv4_a, zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.ip_address = self.ipv4_b
        rec.save()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_aaaarecord_update_increments_serial(self):
        rec = AAAARecord.objects.create(name="aaaa-u", ip_address=self.ipv6_a, zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.ip_address = self.ipv6_b
        rec.save()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_cnamerecord_update_increments_serial(self):
        rec = CNAMERecord.objects.create(name="cname-u", alias="old.example.", zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.alias = "new.example."
        rec.save()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_mxrecord_update_increments_serial(self):
        rec = MXRecord.objects.create(name="mx-u", mail_server="mx1.example.", zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.mail_server = "mx2.example."
        rec.save()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_txtrecord_update_increments_serial(self):
        rec = TXTRecord.objects.create(name="txt-u", text="original", zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.text = "updated"
        rec.save()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_ptrrecord_update_increments_serial(self):
        rec = PTRRecord.objects.create(name="ptr-u", ptrdname="host1.example.", zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.ptrdname = "host2.example."
        rec.save()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_srvrecord_update_increments_serial(self):
        rec = SRVRecord.objects.create(
            name="_sip._tcp.u", priority=10, weight=5, port=5060, target="sip1.example.", zone=self.zone
        )
        base = self._serial_after_create_and_reset()
        rec.target = "sip2.example."
        rec.save()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_record_zone_change_increments_both_zones(self):
        """Moving a record bumps the old zone and the new zone."""
        other = _create_zone("move-target.example", serial=100)
        rec = TXTRecord.objects.create(name="move", text="moving", zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.zone = other
        rec.save()
        self.assertEqual(_refresh_serial(self.zone), base + 1)
        self.assertEqual(_refresh_serial(other), 101)


# ── per-record-type delete tests ───────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialDeleteTestCase(TestCase):
    """Test that deleting any record type triggers a serial increment."""

    @classmethod
    def setUpTestData(cls):
        """Create shared zone and IP fixtures."""
        cls.zone = _create_zone("delete-test.example")
        status = Status.objects.get(name="Active")
        namespace = Namespace.objects.get(name="Global")
        Prefix.objects.create(prefix="10.53.0.0/24", namespace=namespace, type="Pool", status=status)
        Prefix.objects.create(prefix="2001:db8:c3::/64", namespace=namespace, type="Pool", status=status)
        cls.ipv4 = IPAddress.objects.create(address="10.53.0.1/32", namespace=namespace, status=status)
        cls.ipv6 = IPAddress.objects.create(address="2001:db8:c3::1/128", namespace=namespace, status=status)

    def setUp(self):
        """Reset zone serial and dedup state before each test."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=0)
        self.zone.refresh_from_db()
        _reset_dirty_zones_for_testing()

    def _serial_after_create_and_reset(self):
        """Return serial after create and reset dedup state for the delete assertion."""
        serial = _refresh_serial(self.zone)
        _reset_dirty_zones_for_testing()
        return serial

    def test_nsrecord_delete_increments_serial(self):
        rec = NSRecord.objects.create(name="ns-d", server="ns.del-test.example.", zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.delete()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_arecord_delete_increments_serial(self):
        rec = ARecord.objects.create(name="a-d", ip_address=self.ipv4, zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.delete()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_aaaarecord_delete_increments_serial(self):
        rec = AAAARecord.objects.create(name="aaaa-d", ip_address=self.ipv6, zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.delete()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_cnamerecord_delete_increments_serial(self):
        rec = CNAMERecord.objects.create(name="cname-d", alias="gone.example.", zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.delete()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_mxrecord_delete_increments_serial(self):
        rec = MXRecord.objects.create(name="mx-d", mail_server="mail.del.example.", zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.delete()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_txtrecord_delete_increments_serial(self):
        rec = TXTRecord.objects.create(name="txt-d", text="to-delete", zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.delete()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_ptrrecord_delete_increments_serial(self):
        rec = PTRRecord.objects.create(name="ptr-d", ptrdname="del.example.", zone=self.zone)
        base = self._serial_after_create_and_reset()
        rec.delete()
        self.assertEqual(_refresh_serial(self.zone), base + 1)

    def test_srvrecord_delete_increments_serial(self):
        rec = SRVRecord.objects.create(
            name="_sip._tcp.d", priority=10, weight=5, port=5060, target="sip.del.example.", zone=self.zone
        )
        base = self._serial_after_create_and_reset()
        rec.delete()
        self.assertEqual(_refresh_serial(self.zone), base + 1)


# ── zone-level field tests ─────────────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialZoneFieldTestCase(TestCase):
    """Test that zone-level watched-field changes trigger serial increment."""

    @classmethod
    def setUpTestData(cls):
        """Create a zone for watched-field tests."""
        cls.zone = _create_zone("zone-field.example")

    def setUp(self):
        """Reset serial and dedup state; refresh zone to current DB state."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=0)
        self.zone.refresh_from_db()
        _reset_dirty_zones_for_testing()

    def _assert_field_increments(self, field, new_value):
        """Assert that saving zone with a changed watched field bumps serial by 1."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=0)
        self.zone.refresh_from_db()
        _reset_dirty_zones_for_testing()
        setattr(self.zone, field, new_value)
        self.zone.save()
        self.assertEqual(
            _refresh_serial(self.zone),
            1,
            f"Expected serial increment after changing '{field}'",
        )

    def test_watched_fields_trigger_increment(self):
        watched = {
            "name": "zone-field-renamed.example",
            "ttl": 7200,
            "filename": "zone-field-renamed.example.zone",
            "soa_mname": "ns2.zone-field.example",
            "soa_rname": "newadmin@zone-field.example",
            "soa_refresh": 43200,
            "soa_retry": 3600,
            "soa_expire": 1800000,
            "soa_minimum": 7200,
        }
        for field, value in watched.items():
            with self.subTest(field=field):
                self._assert_field_increments(field, value)

    def test_unwatched_field_does_not_increment(self):
        """description is not in _SOA_SERIAL_WATCHED_FIELDS and must not bump serial."""
        self.zone.description = "updated description"
        self.zone.save()
        self.assertEqual(_refresh_serial(self.zone), 0)

    def test_zone_create_does_not_increment(self):
        """Creating a new zone must not trigger auto-increment."""
        new_zone = _create_zone("zone-create-test.example", serial=5)
        self.assertEqual(_refresh_serial(new_zone), 5)

    def test_no_increment_when_watched_field_value_unchanged(self):
        """Saving with update_fields containing a watched field but no value change must not bump serial."""
        original_refresh = self.zone.soa_refresh
        self.zone.save(update_fields=["soa_refresh"])
        self.assertEqual(_refresh_serial(self.zone), 0, "No-op update_fields save must not increment serial")
        self.assertEqual(self.zone.soa_refresh, original_refresh)

    def test_empty_update_fields_is_a_noop(self):
        """save(update_fields=[]) must not write anything or increment serial."""
        self.zone.soa_refresh = 99999  # change in memory but do not persist
        self.zone.save(update_fields=[])
        self.assertEqual(_refresh_serial(self.zone), 0, "empty update_fields must not increment serial")
        self.zone.refresh_from_db()
        self.assertNotEqual(self.zone.soa_refresh, 99999, "empty update_fields must not persist field changes")

    def test_user_submitted_serial_is_overridden_by_auto_increment(self):
        """A user-supplied soa_serial combined with a watched-field change is replaced by auto-increment."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=5)
        self.zone.refresh_from_db()
        _reset_dirty_zones_for_testing()

        self.zone.soa_serial = 999  # user-supplied value
        self.zone.soa_refresh = 43200  # watched field change triggers increment
        self.zone.save()

        # Serial must be 6 (5 + 1 via auto-increment), not 999
        self.assertEqual(_refresh_serial(self.zone), 6, "auto-increment must override the user-supplied serial")

    def test_full_save_without_watched_change_uses_locked_serial(self):
        """A full zone save with no DNS-data change leaves serial at the current DB value."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=7)
        # self.zone still has the stale in-memory value from setUp (serial=0)
        # Change only description — no watched DNS field
        self.zone.description = "only description changed"
        self.zone.save()

        # Serial must remain 7 (the locked DB value), not 0 (stale in-memory)
        self.assertEqual(_refresh_serial(self.zone), 7, "full save must not overwrite a newer DB serial")

    def test_reentrant_serial_write_passes_through_unchanged(self):
        """Internal increment writes (update_fields=['soa_serial']) bypass the auto-increment logic."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=0)
        self.zone.refresh_from_db()
        _reset_dirty_zones_for_testing()

        self.zone.soa_serial = 42
        self.zone.save(update_fields=["soa_serial"])

        self.assertEqual(_refresh_serial(self.zone), 42, "re-entrant serial write must persist the supplied value")

    def test_generator_update_fields_is_normalized_and_applied(self):
        """save(update_fields=generator) must not exhaust the generator before Django writes the field."""
        _reset_dirty_zones_for_testing()
        self.zone.soa_refresh = 43200
        self.zone.save(update_fields=(f for f in ["soa_refresh"]))

        self.zone.refresh_from_db()
        self.assertEqual(self.zone.soa_refresh, 43200, "generator update_fields must persist the field")
        self.assertEqual(
            _refresh_serial(self.zone), 1, "generator update_fields with watched field must increment serial"
        )

    def test_empty_generator_update_fields_is_a_noop(self):
        """save(update_fields=empty_generator) must not write anything or increment serial."""
        self.zone.soa_refresh = 99999
        self.zone.save(update_fields=(f for f in []))
        self.assertEqual(_refresh_serial(self.zone), 0, "empty generator update_fields must not increment serial")
        self.zone.refresh_from_db()
        self.assertNotEqual(
            self.zone.soa_refresh, 99999, "empty generator update_fields must not persist field changes"
        )

    def test_stale_instance_cannot_overwrite_newer_serial(self):
        """A stale in-memory zone instance must never silently overwrite a newer DB serial."""
        # Set DB serial to 5 directly; self.zone is stale (still soa_serial=0 from setUp).
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=5)

        # Full save of only an unwatched field via stale instance must not drop serial to 0.
        _reset_dirty_zones_for_testing()
        self.zone.description = "description only"
        self.zone.save()
        self.assertEqual(_refresh_serial(self.zone), 5, "unwatched full save must not overwrite newer DB serial")

        # Reset: refresh instance so watched fields match DB, then simulate a concurrent
        # serial-only increment to make self.zone stale on soa_serial only.
        self.zone.refresh_from_db()
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=8)
        # self.zone still has soa_serial=5 in memory (stale), all watched fields current.

        # Watched save via stale instance must increment from DB serial (8), not stale (5).
        # Use 3600 (differs from default 7200) so the field change is detected.
        _reset_dirty_zones_for_testing()
        self.zone.soa_retry = 3600
        self.zone.save()
        self.assertEqual(_refresh_serial(self.zone), 9, "watched save must increment from DB serial (8), not stale (5)")

    @override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False)
    def test_no_increment_when_config_disabled(self):
        """No serial bump when SOA_SERIAL_AUTO_INCREMENT is off."""
        TXTRecord.objects.create(name="disabled", text="nope", zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 0)

    def test_increment_only_affects_parent_zone(self):
        """Serial bump must not propagate to unrelated zones."""
        other = _create_zone("other-zone.example", serial=100)
        TXTRecord.objects.create(name="iso", text="isolated", zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 1)
        self.assertEqual(_refresh_serial(other), 100)


# ── deferred-field snapshot tests ──────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialDeferredFieldTestCase(TestCase):
    """Test _initial_soa_serial capture under deferred loads and partial refreshes.

    ``DNSZone.from_db()`` must not touch ``soa_serial`` when it was not selected: doing so
    fires one extra query per instance and removes the field from Django's deferred set,
    which makes a later partial ``refresh_from_db(fields=[...])`` overwrite the caller's
    in-memory value.
    """

    @classmethod
    def setUpTestData(cls):
        """Create a zone with a known non-default serial."""
        cls.zone = _create_zone("deferred.example", serial=42)

    def setUp(self):
        """Reset serial and dedup state."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=42)
        _reset_dirty_zones_for_testing()

    def test_only_leaves_serial_deferred(self):
        """.only() must not trigger a deferred load of soa_serial inside from_db()."""
        zone = DNSZone.objects.only("name").get(pk=self.zone.pk)
        self.assertIn("soa_serial", zone.get_deferred_fields(), "from_db() must not load a deferred soa_serial")
        self.assertIsNone(zone._initial_soa_serial, "an unloaded serial must snapshot as None")

    def test_defer_leaves_serial_deferred(self):
        """.defer('soa_serial') must not trigger a deferred load inside from_db()."""
        zone = DNSZone.objects.defer("soa_serial").get(pk=self.zone.pk)
        self.assertIn("soa_serial", zone.get_deferred_fields())
        self.assertIsNone(zone._initial_soa_serial)

    def test_deferred_queryset_does_not_issue_n_plus_one(self):
        """A deferred queryset must not cost one extra query per row.

        Asserted as "cost does not grow with row count" rather than an absolute number, so the
        test stays valid regardless of any fixed per-test query overhead.
        """
        one = DNSZone.objects.filter(pk=self.zone.pk)
        with CaptureQueriesContext(connection) as single:
            self.assertEqual(len(list(one.defer("soa_serial"))), 1)

        extra_pks = [_create_zone(f"deferred-n{i}.example", serial=10 + i).pk for i in range(4)]
        many = DNSZone.objects.filter(pk__in=[self.zone.pk, *extra_pks])
        with CaptureQueriesContext(connection) as multi:
            self.assertEqual(len(list(many.defer("soa_serial"))), 5)

        self.assertEqual(
            len(multi.captured_queries),
            len(single.captured_queries),
            "deferred iteration must not scale with row count (N+1 in DNSZone.from_db)",
        )

    def test_deferred_access_restores_snapshot(self):
        """Reading a deferred soa_serial must populate the snapshot so clean() still validates."""
        zone = DNSZone.objects.defer("soa_serial").get(pk=self.zone.pk)
        self.assertIsNone(zone._initial_soa_serial)
        self.assertEqual(zone.soa_serial, 42, "deferred read loads the real value")
        self.assertEqual(zone._initial_soa_serial, 42, "deferred read must restore the snapshot")

        zone.soa_serial = 999
        with self.assertRaises(ValidationError):
            zone.clean()

    def test_deferred_direct_assignment_is_rejected(self):
        """Assigning a deferred serial must still compare against the persisted DB value."""
        zone = DNSZone.objects.only("name").get(pk=self.zone.pk)
        self.assertIn("soa_serial", zone.get_deferred_fields())

        zone.soa_serial = 999
        self.assertNotIn("soa_serial", zone.get_deferred_fields())
        with self.assertRaises(ValidationError):
            zone.full_clean()

    def test_clean_with_unloaded_deferred_serial_does_not_force_load(self):
        """clean() must not load soa_serial merely because it remains deferred and unchanged."""
        zone = DNSZone.objects.only("name").get(pk=self.zone.pk)

        zone.clean()

        self.assertIn("soa_serial", zone.get_deferred_fields())
        self.assertIsNone(zone._initial_soa_serial)

    def test_partial_refresh_excluding_serial_preserves_in_memory_value(self):
        """refresh_from_db(fields=[...]) must not touch soa_serial when it was not requested."""
        zone = DNSZone.objects.get(pk=self.zone.pk)
        zone.soa_serial = 999
        zone.refresh_from_db(fields=["name"])
        self.assertEqual(zone.soa_serial, 999, "partial refresh must not clobber an excluded field")
        self.assertEqual(zone._initial_soa_serial, 42, "snapshot must still reflect the DB-loaded value")

        with self.assertRaises(ValidationError):
            zone.clean()

    def test_partial_refresh_including_serial_updates_snapshot(self):
        """refresh_from_db(fields=['soa_serial']) must re-snapshot the refreshed value."""
        zone = DNSZone.objects.get(pk=self.zone.pk)
        DNSZone.objects.filter(pk=zone.pk).update(soa_serial=77)
        zone.refresh_from_db(fields=["soa_serial"])
        self.assertEqual(zone.soa_serial, 77)
        self.assertEqual(zone._initial_soa_serial, 77)
        zone.clean()  # must not raise: the value matches what was loaded

    def test_full_refresh_updates_snapshot(self):
        """A full refresh_from_db() must re-snapshot, discarding an unsaved manual edit."""
        zone = DNSZone.objects.get(pk=self.zone.pk)
        zone.soa_serial = 999
        zone.refresh_from_db()
        self.assertEqual(zone.soa_serial, 42)
        self.assertEqual(zone._initial_soa_serial, 42)
        zone.clean()  # must not raise

    def test_generator_refresh_fields_are_normalized(self):
        """A generator passed as fields= must not be consumed before the membership check."""
        zone = DNSZone.objects.get(pk=self.zone.pk)
        DNSZone.objects.filter(pk=zone.pk).update(soa_serial=88)
        zone.refresh_from_db(fields=(f for f in ["soa_serial"]))
        self.assertEqual(zone._initial_soa_serial, 88, "generator fields must still be inspected")

        other = DNSZone.objects.get(pk=self.zone.pk)
        other.soa_serial = 999
        other.refresh_from_db(fields=(f for f in ["name"]))
        self.assertEqual(other.soa_serial, 999, "generator fields excluding serial must not clobber it")

    def test_manual_serial_change_still_rejected_after_full_load(self):
        """The ordinary (non-deferred) rejection path must be unaffected by the guard."""
        zone = DNSZone.objects.get(pk=self.zone.pk)
        self.assertEqual(zone._initial_soa_serial, 42)
        zone.soa_serial = 43
        with self.assertRaises(ValidationError):
            zone.clean()


# ── coalescing tests ───────────────────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialBulkCoalescingTestCase(TestCase):
    """Test that multiple record changes in one atomic block produce one serial bump."""

    @classmethod
    def setUpTestData(cls):
        """Create shared zone for coalescing tests."""
        cls.zone = _create_zone("bulk-test.example")

    def setUp(self):
        """Reset serial and dedup state before each test."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=0)
        self.zone.refresh_from_db()
        _reset_dirty_zones_for_testing()

    def test_bulk_creates_coalesce_to_one_increment(self):
        """Multiple creates in one atomic block must produce exactly one serial bump."""
        _reset_dirty_zones_for_testing()
        try:
            with transaction.atomic():
                TXTRecord.objects.create(name="bulk1", text="t1", zone=self.zone)
                TXTRecord.objects.create(name="bulk2", text="t2", zone=self.zone)
                TXTRecord.objects.create(name="bulk3", text="t3", zone=self.zone)
        finally:
            _reset_dirty_zones_for_testing()
        self.assertEqual(_refresh_serial(self.zone), 1)

    def test_mixed_record_types_coalesce(self):
        """Creates of different record types in one atomic block coalesce to one bump."""
        _reset_dirty_zones_for_testing()
        try:
            with transaction.atomic():
                TXTRecord.objects.create(name="mix-txt", text="mixed", zone=self.zone)
                NSRecord.objects.create(name="mix-ns", server="ns.example.", zone=self.zone)
                CNAMERecord.objects.create(name="mix-cname", alias="target.example.", zone=self.zone)
        finally:
            _reset_dirty_zones_for_testing()
        self.assertEqual(_refresh_serial(self.zone), 1)

    def test_multi_zone_transaction_independent_increments(self):
        """Each zone gets one bump; two zones in one atomic block each get one increment."""
        zone2 = _create_zone("bulk-zone2.example")
        _reset_dirty_zones_for_testing()
        try:
            with transaction.atomic():
                TXTRecord.objects.create(name="z1-r1", text="a", zone=self.zone)
                TXTRecord.objects.create(name="z2-r1", text="b", zone=zone2)
                TXTRecord.objects.create(name="z1-r2", text="c", zone=self.zone)
        finally:
            _reset_dirty_zones_for_testing()
        self.assertEqual(_refresh_serial(self.zone), 1)
        self.assertEqual(_refresh_serial(zone2), 1)


# ── record mutation policy ─────────────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialRecordMutationTestCase(TestCase):
    """Test that any persisted DNS record modification bumps the serial."""

    @classmethod
    def setUpTestData(cls):
        """Create shared zone for metadata tests."""
        cls.zone = _create_zone("record-metadata.example")

    def setUp(self):
        """Create a record, then reset serial and dedup state before each test."""
        self.record = TXTRecord.objects.create(name="meta", text="payload", zone=self.zone)
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=100)
        self.zone.refresh_from_db()
        _reset_dirty_zones_for_testing()

    def _form_data(self, **overrides):
        """Return a valid TXTRecordForm payload for the shared record."""
        data = {
            "name": self.record.name,
            "text": self.record.text,
            "ttl": self.record._ttl or self.zone.ttl,
            "zone": self.zone,
            "description": self.record.description,
            "comment": self.record.comment,
        }
        data.update(overrides)
        return data

    def test_description_only_update_increments(self):
        """A description-only save is still a persisted record modification."""
        self.record.description = "internal note"
        self.record.save(update_fields=["description"])
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_comment_only_update_increments(self):
        """A comment-only save is still a persisted record modification."""
        self.record.comment = "ticket JIRA-1234"
        self.record.save(update_fields=["comment"])
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_description_and_comment_together_increment_once(self):
        """A save touching both record note fields increments once."""
        self.record.description = "note"
        self.record.comment = "ticket"
        self.record.save(update_fields=["description", "comment"])
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_generator_update_fields_is_normalized_and_increments(self):
        """A generator update_fields is normalized before saving and incrementing."""
        self.record.comment = "generated"
        self.record.save(update_fields=(field for field in ["comment"]))
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_metadata_alongside_served_field_increments(self):
        """Record note fields mixed with served data still increment once."""
        self.record.comment = "ticket"
        self.record.text = "changed"
        self.record.save(update_fields=["comment", "text"])
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_served_field_only_update_increments(self):
        """A served-data change still increments."""
        self.record.text = "changed"
        self.record.save(update_fields=["text"])
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_ttl_update_increments(self):
        """TTL is served zone data and must increment."""
        self.record._ttl = 900
        self.record.save(update_fields=["_ttl"])
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_bare_save_still_increments(self):
        """A save without update_fields always increments.

        No-op detection is deliberately not implemented: it would require snapshotting
        every served field at load time across every record subclass.
        """
        self.record.save()
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_modelform_description_and_comment_update_increments(self):
        """The normal UI ModelForm path saves without update_fields and increments."""
        form = TXTRecordForm(
            data=self._form_data(description="form note", comment="form ticket"),
            instance=self.record,
        )
        self.assertTrue(form.is_valid(), f"form must be valid: {form.errors}")
        form.save()

        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_zone_move_with_metadata_still_increments_both_zones(self):
        """zone is served data, so a move increments even when bundled with metadata."""
        target = _create_zone("record-metadata-target.example")
        DNSZone.objects.filter(pk=target.pk).update(soa_serial=200)
        target.refresh_from_db()
        _reset_dirty_zones_for_testing()

        self.record.zone = target
        self.record.comment = "moved"
        self.record.save(update_fields=["zone", "comment"])

        self.assertEqual(_refresh_serial(self.zone), 101)
        self.assertEqual(_refresh_serial(target), 201)


# ── bulk delete via QuerySet.delete() ──────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialBulkDeleteTestCase(TestCase):
    """Test that QuerySet.delete() increments the serial.

    QuerySet.delete() never calls Model.delete(), and it is the path the Nautobot
    bulk-delete views take, so these cover the "Delete Selected" UI action.
    """

    @classmethod
    def setUpTestData(cls):
        """Create shared zone and IP fixtures for bulk delete tests."""
        cls.zone = _create_zone("bulk-delete.example")
        status = Status.objects.get(name="Active")
        namespace = Namespace.objects.get(name="Global")
        Prefix.objects.create(prefix="10.54.0.0/24", namespace=namespace, type="Pool", status=status)
        cls.ipv4 = IPAddress.objects.create(address="10.54.0.1/32", namespace=namespace, status=status)

    def _reset_serial(self, *zones):
        """Pin the given zones to serial 600 and clear dedup state."""
        pks = [zone.pk for zone in zones] or [self.zone.pk]
        DNSZone.objects.filter(pk__in=pks).update(soa_serial=600)
        for zone in zones or (self.zone,):
            zone.refresh_from_db()
        _reset_dirty_zones_for_testing()

    def setUp(self):
        """Reset serial and dedup state before each test."""
        self._reset_serial()

    def test_queryset_delete_increments_serial(self):
        """QuerySet.delete() must increment; this is the reported bulk-delete gap."""
        TXTRecord.objects.create(name="qs-del", text="x", zone=self.zone)
        self._reset_serial()

        TXTRecord.objects.filter(name="qs-del").delete()

        self.assertEqual(_refresh_serial(self.zone), 601)

    def test_queryset_delete_of_many_coalesces_to_one_increment(self):
        """Deleting several records in one operation coalesces to a single bump."""
        for index in range(5):
            TXTRecord.objects.create(name=f"qs-multi-{index}", text="x", zone=self.zone)
        self._reset_serial()

        TXTRecord.objects.filter(name__startswith="qs-multi-").delete()

        self.assertEqual(_refresh_serial(self.zone), 601)

    def test_model_delete_increments_exactly_once(self):
        """Regression guard: the receiver must not double-increment with Model.delete()."""
        record = TXTRecord.objects.create(name="single-del", text="x", zone=self.zone)
        self._reset_serial()

        record.delete()

        self.assertEqual(_refresh_serial(self.zone), 601)

    def test_queryset_delete_across_zones_increments_each(self):
        """A bulk delete spanning two zones increments both."""
        zone2 = _create_zone("bulk-delete-2.example")
        TXTRecord.objects.create(name="span-1", text="x", zone=self.zone)
        TXTRecord.objects.create(name="span-2", text="x", zone=zone2)
        self._reset_serial(self.zone, zone2)

        TXTRecord.objects.filter(name__in=["span-1", "span-2"]).delete()

        self.assertEqual(_refresh_serial(self.zone), 601)
        self.assertEqual(_refresh_serial(zone2), 601)

    def test_queryset_delete_does_not_increment_when_disabled(self):
        """With auto-increment disabled the serial is untouched."""
        TXTRecord.objects.create(name="qs-off", text="x", zone=self.zone)
        self._reset_serial()

        with override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False):
            TXTRecord.objects.filter(name="qs-off").delete()

        self.assertEqual(_refresh_serial(self.zone), 600)

    def test_cascade_delete_increments_serial(self):
        """A record removed by cascade from its IP address still increments."""
        ARecord.objects.create(name="cascade-a", ip_address=self.ipv4, zone=self.zone)
        self._reset_serial()

        self.ipv4.delete()

        self.assertEqual(_refresh_serial(self.zone), 601)


# ── failed delete cleanup (TransactionTestCase) ───────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialFailedDeleteCleanupTestCase(TransactionTestCase):
    """A rolled-back delete batch must not leak captured zone IDs to the next delete."""

    abort_dispatch_uid = "nautobot_dns_models.tests.abort_txt_delete_after_capture"

    def setUp(self):
        """Create two zones and clear dedup state."""
        _reset_dirty_zones_for_testing()
        self.zone_a = _create_zone("failed-delete-a.example")
        self.zone_b = _create_zone("failed-delete-b.example")

    def tearDown(self):
        """Disconnect the test receiver and verify pending delete state did not leak."""
        pre_delete.disconnect(sender=TXTRecord, dispatch_uid=self.abort_dispatch_uid)
        _reset_dirty_zones_for_testing()

    def test_failed_delete_does_not_increment_stale_zone_on_next_delete(self):
        """A failed delete of zone A's record must not make zone A increment on zone B's delete."""
        record_a = TXTRecord.objects.create(name="failed-delete-a", text="a", zone=self.zone_a)
        record_b = TXTRecord.objects.create(name="failed-delete-b", text="b", zone=self.zone_b)
        DNSZone.objects.filter(pk__in=[self.zone_a.pk, self.zone_b.pk]).update(soa_serial=100)
        _reset_dirty_zones_for_testing()

        def _abort_after_capture(sender, instance, **kwargs):  # pylint: disable=unused-argument
            if instance.pk == record_a.pk:
                raise RuntimeError("abort delete after SOA serial pre_delete capture")

        pre_delete.connect(_abort_after_capture, sender=TXTRecord, dispatch_uid=self.abort_dispatch_uid)
        with self.assertRaises(RuntimeError):
            TXTRecord.objects.filter(pk=record_a.pk).delete()
        pre_delete.disconnect(sender=TXTRecord, dispatch_uid=self.abort_dispatch_uid)

        self.assertTrue(TXTRecord.objects.filter(pk=record_a.pk).exists(), "failed delete must roll back")
        self.assertEqual(_refresh_serial(self.zone_a), 100)
        self.assertEqual(_refresh_serial(self.zone_b), 100)

        TXTRecord.objects.filter(pk=record_b.pk).delete()

        self.assertEqual(_refresh_serial(self.zone_a), 100, "stale zone A capture must not be consumed")
        self.assertEqual(_refresh_serial(self.zone_b), 101, "zone B delete must still increment")


# ── rollover test ──────────────────────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialRolloverTestCase(TestCase):
    """Test RFC 2136 §7.11 rollover: UINT32_MAX wraps to 1, not 0."""

    def test_serial_rolls_over_to_one_at_uint32_max(self):
        """Serial at UINT32_MAX must roll over to 1 (RFC 2136 §7.11)."""
        zone = _create_zone("rollover.example", serial=UINT32_MAX)
        _reset_dirty_zones_for_testing()
        TXTRecord.objects.create(name="roll", text="trigger", zone=zone)
        self.assertEqual(_refresh_serial(zone), 1)


# ── rollback isolation (TransactionTestCase) ───────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialRollbackIsolationTestCase(TransactionTestCase):
    """Verify that rolling back a transaction does not leak dedup state."""

    def setUp(self):
        """Create zone and clear dedup state."""
        _reset_dirty_zones_for_testing()
        self.zone = _create_zone("rollback-isolation.example")

    def tearDown(self):
        """Clear dedup state after each test."""
        _reset_dirty_zones_for_testing()

    def test_rollback_clears_dedup_state(self):
        """A rolled-back outer atomic must not block the next transaction's increment."""
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                TXTRecord.objects.create(name="rb-1", text="first", zone=self.zone)
                raise RuntimeError("intentional rollback")

        self.zone.refresh_from_db()
        self.assertEqual(self.zone.soa_serial, 0, "rolled-back bump must not persist")
        self.assertFalse(TXTRecord.objects.filter(name="rb-1").exists(), "rb-1 must not exist after rollback")

        with transaction.atomic():
            TXTRecord.objects.create(name="rb-2", text="second", zone=self.zone)

        self.zone.refresh_from_db()
        self.assertEqual(
            self.zone.soa_serial, 1, "second transaction must increment — dedup state leaked across rollback"
        )
        self.assertTrue(TXTRecord.objects.filter(name="rb-2").exists(), "rb-2 must be persisted")


# ── thread-reuse isolation (TransactionTestCase) ───────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialThreadReuseTestCase(TransactionTestCase):
    """Two sequential atomic blocks separated by connection.close() must both bump serial."""

    def setUp(self):
        """Create zone and clear dedup state."""
        _reset_dirty_zones_for_testing()
        self.zone = _create_zone("thread-reuse.example")

    def tearDown(self):
        """Clear dedup state after each test."""
        _reset_dirty_zones_for_testing()

    def test_back_to_back_transactions_both_increment(self):
        """Simulate worker-thread reuse: each new request must get a fresh dedup set."""
        with transaction.atomic():
            TXTRecord.objects.create(name="reuse-1", text="first", zone=self.zone)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.soa_serial, 1)

        connection.close()

        with transaction.atomic():
            TXTRecord.objects.create(name="reuse-2", text="second", zone=self.zone)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.soa_serial, 2)


# ── nested-savepoint rollback isolation (TransactionTestCase) ─────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialNestedSavepointTestCase(TransactionTestCase):
    """A zone mutated inside a rolled-back savepoint must still receive one increment on the next mutation."""

    def setUp(self):
        """Create two zones and clear dedup state."""
        _reset_dirty_zones_for_testing()
        self.zone_a = _create_zone("sp-zone-a.example")
        self.zone_b = _create_zone("sp-zone-b.example")

    def tearDown(self):
        """Clear dedup state after each test."""
        _reset_dirty_zones_for_testing()

    def test_savepoint_rollback_does_not_suppress_later_zone_increment(self):
        """Zone B mutated in a rolled-back savepoint must still increment on the next mutation."""
        with transaction.atomic():
            # Outer: mutate zone A — serial becomes 1.
            TXTRecord.objects.create(name="sp-a-1", text="zone-a", zone=self.zone_a)

            # Inner savepoint: mutate zone B, then roll back.
            try:
                with transaction.atomic():
                    TXTRecord.objects.create(name="sp-b-rolled", text="zone-b", zone=self.zone_b)
                    raise RuntimeError("intentional savepoint rollback")
            except RuntimeError:
                pass

            # Outer continues: mutate zone B again — dedup state must not suppress this.
            TXTRecord.objects.create(name="sp-b-committed", text="zone-b-ok", zone=self.zone_b)

        self.zone_a.refresh_from_db()
        self.assertEqual(self.zone_a.soa_serial, 1, "zone A must have been incremented once")

        self.zone_b.refresh_from_db()
        self.assertEqual(
            self.zone_b.soa_serial, 1, "zone B must increment — savepoint rollback must not leave it marked dirty"
        )

        self.assertFalse(TXTRecord.objects.filter(name="sp-b-rolled").exists(), "rolled-back record must not persist")
        self.assertTrue(TXTRecord.objects.filter(name="sp-b-committed").exists(), "committed record must persist")


# ── concurrency tests (PostgreSQL only) ───────────────────────────────────────


def _run_concurrently(*fns):
    """Run each callable in its own thread on its own DB connection; re-raise the first failure.

    Each thread closes its connection on exit so the test runner does not inherit it.
    """
    errors = []
    barrier = threading.Barrier(len(fns))

    def _wrap(fn):
        def _inner():
            try:
                barrier.wait(timeout=10)  # maximise overlap: no thread proceeds until all are ready
                fn()
            # Deliberately broad: any thread failure is captured and re-raised on the main thread.
            except Exception as exc:  # pylint: disable=broad-exception-caught
                errors.append(exc)
            finally:
                connection.close()

        return _inner

    threads = [threading.Thread(target=_wrap(fn)) for fn in fns]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    for thread in threads:
        if thread.is_alive():
            raise AssertionError("concurrent thread did not finish within 30s (possible deadlock)")
    if errors:
        raise errors[0]


@skipUnless(connection.vendor == "postgresql", "row-level locking semantics require PostgreSQL")
@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialConcurrencyTestCase(TransactionTestCase):
    """Verify select_for_update() prevents lost serial updates under real concurrency.

    SQLite cannot express row-level locking, so these are skipped outside PostgreSQL rather
    than run and silently pass. Requires TransactionTestCase: each thread needs a real
    committed transaction, which TestCase's wrapping atomic would prevent.
    """

    def setUp(self):
        """Create two zones and clear dedup state."""
        _reset_dirty_zones_for_testing()
        self.zone_a = _create_zone("concurrent-a.example")
        self.zone_b = _create_zone("concurrent-b.example")

    def tearDown(self):
        """Clear dedup state after each test."""
        _reset_dirty_zones_for_testing()

    def test_concurrent_record_creates_do_not_lose_updates(self):
        """Two transactions adding records to one zone must produce exactly two increments."""

        def _add(name):
            def _fn():
                with transaction.atomic():
                    TXTRecord.objects.create(name=name, text=name, zone=self.zone_a)

            return _fn

        _run_concurrently(_add("concurrent-1"), _add("concurrent-2"))

        self.zone_a.refresh_from_db()
        self.assertEqual(
            self.zone_a.soa_serial,
            2,
            "both committed transactions must be reflected — a lost update means select_for_update() is not holding",
        )

    def test_opposing_record_moves_do_not_deadlock(self):
        """Records moving between two zones in opposite directions must not deadlock.

        Both directions touch zone_a and zone_b, so a naive lock order would let the two
        transactions each hold what the other needs.
        """
        rec_a = TXTRecord.objects.create(name="mover-a", text="a", zone=self.zone_a)
        rec_b = TXTRecord.objects.create(name="mover-b", text="b", zone=self.zone_b)
        DNSZone.objects.filter(pk__in=[self.zone_a.pk, self.zone_b.pk]).update(soa_serial=0)
        _reset_dirty_zones_for_testing()

        def _move(record, target_zone):
            def _fn():
                with transaction.atomic():
                    record.zone = target_zone
                    record.save()

            return _fn

        # Raises AssertionError on timeout rather than hanging the suite.
        _run_concurrently(_move(rec_a, self.zone_b), _move(rec_b, self.zone_a))

        self.zone_a.refresh_from_db()
        self.zone_b.refresh_from_db()
        # Each move increments both the old and the new zone, so each zone is touched twice.
        self.assertEqual(self.zone_a.soa_serial, 2, "zone A must record both the departure and the arrival")
        self.assertEqual(self.zone_b.soa_serial, 2, "zone B must record both the departure and the arrival")

    def test_concurrent_zone_update_and_record_create(self):
        """A watched zone-field update racing a record create must yield two increments."""

        def _update_zone():
            with transaction.atomic():
                zone = DNSZone.objects.get(pk=self.zone_a.pk)
                zone.soa_retry = 3600
                zone.save()

        def _add_record():
            with transaction.atomic():
                TXTRecord.objects.create(name="race-record", text="race", zone=self.zone_a)

        _run_concurrently(_update_zone, _add_record)

        self.zone_a.refresh_from_db()
        self.assertEqual(
            self.zone_a.soa_serial,
            2,
            "the zone-field change and the record create must each contribute one increment",
        )

    def test_concurrent_queryset_deletes_across_same_zones_do_not_deadlock(self):
        """Concurrent bulk-delete transactions touching the same zones in opposite order must finish."""
        first_a = TXTRecord.objects.create(name="delete-first-a", text="a", zone=self.zone_a)
        first_b = TXTRecord.objects.create(name="delete-first-b", text="b", zone=self.zone_b)
        second_b = TXTRecord.objects.create(name="delete-second-b", text="b", zone=self.zone_b)
        second_a = TXTRecord.objects.create(name="delete-second-a", text="a", zone=self.zone_a)
        DNSZone.objects.filter(pk__in=[self.zone_a.pk, self.zone_b.pk]).update(soa_serial=0)
        _reset_dirty_zones_for_testing()

        def _delete_ab():
            with transaction.atomic():
                TXTRecord.objects.filter(pk__in=[first_a.pk, first_b.pk]).delete()

        def _delete_ba():
            with transaction.atomic():
                TXTRecord.objects.filter(pk__in=[second_b.pk, second_a.pk]).delete()

        _run_concurrently(_delete_ab, _delete_ba)

        self.zone_a.refresh_from_db()
        self.zone_b.refresh_from_db()
        self.assertEqual(self.zone_a.soa_serial, 2)
        self.assertEqual(self.zone_b.soa_serial, 2)


# ── REST API serial increment tests ───────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialAPITestCase(APIViewTestCases.APIViewTestCase):
    """Test that record CRUD via the REST API triggers serial increments."""

    model = TXTRecord
    view_namespace = "plugins-api:nautobot_dns_models"
    brief_fields = ["name", "text"]

    @classmethod
    def setUpTestData(cls):
        """Create zones and seed records required by the APIViewTestCase base."""
        cls.api_zone = _create_zone("api-serial-test.example")
        cls.api_zone2 = _create_zone("api-serial-test2.example")
        # Seed three records so APIViewTestCases base tests can run.
        TXTRecord.objects.create(name="seed-1", text="seed", zone=cls.api_zone)
        TXTRecord.objects.create(name="seed-2", text="seed", zone=cls.api_zone)
        TXTRecord.objects.create(name="seed-3", text="seed", zone=cls.api_zone)
        cls.create_data = [
            {"name": "api-c1", "text": "val1", "zone": cls.api_zone.id},
            {"name": "api-c2", "text": "val2", "zone": cls.api_zone.id},
            {"name": "api-c3", "text": "val3", "zone": cls.api_zone.id},
        ]

    def setUp(self):
        """Reset serial and dedup state before each test."""
        super().setUp()
        DNSZone.objects.filter(pk=self.api_zone.pk).update(soa_serial=0)
        DNSZone.objects.filter(pk=self.api_zone2.pk).update(soa_serial=0)
        self.api_zone.refresh_from_db()
        self.api_zone2.refresh_from_db()
        _reset_dirty_zones_for_testing()

    def test_api_post_increments_serial(self):
        """POST to create a record via the API must increment the zone serial."""
        self.add_permissions("nautobot_dns_models.add_txtrecord", "nautobot_dns_models.view_dnszone")
        url = reverse("plugins-api:nautobot_dns_models-api:txtrecord-list")
        data = {"name": "api-new", "text": "created via api", "zone": self.api_zone.id}
        response = self.client.post(url, data=data, format="json", **self.header)
        self.assertHttpStatus(response, http_status.HTTP_201_CREATED)
        self.assertEqual(_refresh_serial(self.api_zone), 1)

    def test_api_patch_increments_serial(self):
        """PATCH to update a record via the API must increment the zone serial."""
        self.add_permissions(
            "nautobot_dns_models.view_txtrecord",
            "nautobot_dns_models.change_txtrecord",
            "nautobot_dns_models.view_dnszone",
        )
        record = TXTRecord.objects.create(name="api-upd", text="before", zone=self.api_zone)
        _reset_dirty_zones_for_testing()
        DNSZone.objects.filter(pk=self.api_zone.pk).update(soa_serial=0)

        url = reverse("plugins-api:nautobot_dns_models-api:txtrecord-detail", kwargs={"pk": record.pk})
        response = self.client.patch(url, data={"text": "after"}, format="json", **self.header)
        self.assertHttpStatus(response, http_status.HTTP_200_OK)
        self.assertEqual(_refresh_serial(self.api_zone), 1)

    def test_api_patch_description_and_comment_increments_serial(self):
        """PATCHing record note fields through DRF still increments because serializers call save()."""
        self.add_permissions(
            "nautobot_dns_models.view_txtrecord",
            "nautobot_dns_models.change_txtrecord",
            "nautobot_dns_models.view_dnszone",
        )
        record = TXTRecord.objects.create(name="api-note", text="before", zone=self.api_zone)
        _reset_dirty_zones_for_testing()
        DNSZone.objects.filter(pk=self.api_zone.pk).update(soa_serial=0)

        url = reverse("plugins-api:nautobot_dns_models-api:txtrecord-detail", kwargs={"pk": record.pk})
        response = self.client.patch(
            url,
            data={"description": "API note", "comment": "API ticket"},
            format="json",
            **self.header,
        )
        self.assertHttpStatus(response, http_status.HTTP_200_OK)
        self.assertEqual(_refresh_serial(self.api_zone), 1)

    def test_api_delete_increments_serial(self):
        """DELETE a record via the API must increment the zone serial."""
        self.add_permissions(
            "nautobot_dns_models.view_txtrecord",
            "nautobot_dns_models.delete_txtrecord",
            "nautobot_dns_models.view_dnszone",
        )
        record = TXTRecord.objects.create(name="api-del", text="to-delete", zone=self.api_zone)
        _reset_dirty_zones_for_testing()
        DNSZone.objects.filter(pk=self.api_zone.pk).update(soa_serial=0)

        url = reverse("plugins-api:nautobot_dns_models-api:txtrecord-detail", kwargs={"pk": record.pk})
        response = self.client.delete(url, **self.header)
        self.assertHttpStatus(response, http_status.HTTP_204_NO_CONTENT)
        self.assertEqual(_refresh_serial(self.api_zone), 1)

    def test_api_bulk_create_coalesces_to_one_increment_per_zone(self):
        """A single bulk-create API call must coalesce to exactly one serial increment per affected zone."""
        self.add_permissions("nautobot_dns_models.add_txtrecord", "nautobot_dns_models.view_dnszone")
        url = reverse("plugins-api:nautobot_dns_models-api:txtrecord-list")
        data = [
            {"name": "api-bulk-1", "text": "bulk-a", "zone": str(self.api_zone.pk)},
            {"name": "api-bulk-2", "text": "bulk-b", "zone": str(self.api_zone.pk)},
            {"name": "api-bulk-3", "text": "bulk-c", "zone": str(self.api_zone2.pk)},
        ]
        response = self.client.post(url, data=data, format="json", **self.header)
        self.assertHttpStatus(response, http_status.HTTP_201_CREATED)
        self.assertEqual(_refresh_serial(self.api_zone), 1, "zone 1 must increment exactly once for its two records")
        self.assertEqual(_refresh_serial(self.api_zone2), 1, "zone 2 must increment exactly once for its one record")

    def test_api_patch_serial_change_rejected_when_auto_increment_enabled(self):
        """PATCH that changes soa_serial directly must be rejected when auto-increment is on."""
        self.add_permissions(
            "nautobot_dns_models.view_dnszone",
            "nautobot_dns_models.change_dnszone",
        )
        url = reverse("plugins-api:nautobot_dns_models-api:dnszone-detail", kwargs={"pk": self.api_zone.pk})
        response = self.client.patch(url, data={"soa_serial": 999}, format="json", **self.header)
        self.assertHttpStatus(response, http_status.HTTP_400_BAD_REQUEST)
        self.assertIn("soa_serial", response.data)
        self.assertEqual(_refresh_serial(self.api_zone), 0, "serial must not change after rejected PATCH")


# ── form serial-rejection tests ────────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialFormTestCase(TestCase):
    """Test that DNSZoneForm rejects manual serial changes while auto-increment is enabled."""

    @classmethod
    def setUpTestData(cls):
        """Create a zone fixture."""
        cls.zone = _create_zone("form-serial-test.example", serial=5)

    def setUp(self):
        """Reset zone serial and dedup state before each test."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=5)
        self.zone.refresh_from_db()
        _reset_dirty_zones_for_testing()

    def _base_form_data(self, **overrides):
        """Return a minimal valid form payload for the test zone."""
        data = {
            "name": self.zone.name,
            "filename": self.zone.filename,
            "soa_mname": self.zone.soa_mname,
            "soa_rname": self.zone.soa_rname,
            "soa_refresh": self.zone.soa_refresh,
            "soa_retry": self.zone.soa_retry,
            "soa_expire": self.zone.soa_expire,
            "soa_serial": self.zone.soa_serial,
            "soa_minimum": self.zone.soa_minimum,
            "ttl": self.zone.ttl,
            "dns_view": self.zone.dns_view_id,
        }
        data.update(overrides)
        return data

    def test_form_rejects_serial_change_when_auto_increment_enabled(self):
        """DNSZoneForm must be invalid when soa_serial is changed while auto-increment is on."""
        form = DNSZoneForm(data=self._base_form_data(soa_serial=999), instance=self.zone)
        self.assertFalse(form.is_valid(), "form must be invalid when serial changed with auto-increment enabled")
        self.assertIn("soa_serial", form.errors)

    def test_form_accepts_serial_unchanged(self):
        """DNSZoneForm must be valid when soa_serial is submitted unchanged."""
        form = DNSZoneForm(data=self._base_form_data(), instance=self.zone)
        self.assertTrue(form.is_valid(), f"form must be valid when serial is unchanged: {form.errors}")

    @override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False)
    def test_form_allows_serial_change_when_auto_increment_disabled(self):
        """DNSZoneForm must allow manual serial change when auto-increment is off."""
        form = DNSZoneForm(data=self._base_form_data(soa_serial=999), instance=self.zone)
        self.assertTrue(form.is_valid(), f"form must allow serial change when auto-increment is off: {form.errors}")
