"""Tests for SOA serial auto-increment."""

from constance.test import override_config
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.urls import reverse
from nautobot.apps.testing import APIViewTestCases, TestCase
from nautobot.extras.models import Status
from nautobot.ipam.models import IPAddress, Namespace, Prefix
from rest_framework import status as http_status

from nautobot_dns_models.forms import DNSZoneForm
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


def _reset_dirty_zones_for_testing():
    """Clear the connection-bound dedup state between tests."""
    conn = connection
    try:
        delattr(conn, "_dns_dirty_zones")
    except AttributeError:
        pass


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
