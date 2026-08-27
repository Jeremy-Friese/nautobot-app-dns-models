"""Tests for SOA serial auto-increment."""

# pylint: disable=too-many-lines

import threading
from unittest import skipUnless

from constance import config as constance_config
from constance.test import override_config
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import F
from django.db.models.signals import pre_delete
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from nautobot.apps.testing import APITestCase, TestCase, TransactionTestCase
from nautobot.extras.choices import CustomFieldTypeChoices
from nautobot.extras.models import CustomField, Status, Tag
from nautobot.ipam.models import IPAddress, Namespace, Prefix
from rest_framework import status as http_status

from nautobot_dns_models.forms import DNSZoneBulkEditForm, DNSZoneForm, TXTRecordForm
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

# ── module-level helpers ───────────────────────────────────────────────────────


def _refresh_serial(zone):
    """Reload zone from DB and return soa_serial."""
    zone.refresh_from_db()
    return zone.soa_serial


def _create_zone(name, serial=0):
    """Create a DNSZone with the given name and starting serial."""
    return DNSZone.objects.create(
        name=name,
        filename=f"{name}.zone",
        soa_mname=f"ns1.{name}",
        soa_rname=f"admin@{name}",
        soa_serial=serial,
    )


def _create_record_without_bump(model, **kwargs):
    """Create a record with auto-increment disabled so creation does not dirty the zone.

    Use this in tests that isolate a specific mutation (update or delete) as the sole
    serial-incrementing operation.
    """
    with override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False):
        return model.objects.create(**kwargs)


def _reset_zone_serial(zone, serial=0):
    """Update zone serial directly in the DB and refresh the in-memory instance."""
    DNSZone.objects.filter(pk=zone.pk).update(soa_serial=serial)
    zone.refresh_from_db()


# ── per-record-type lifecycle (create / update / delete) ──────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialRecordLifecycleTestCase(TestCase):
    """Test that every record type increments the serial on create, update, and delete."""

    @classmethod
    def setUpTestData(cls):
        """Create shared zone and IP fixtures."""
        cls.zone = _create_zone("lifecycle.example")
        status = Status.objects.get(name="Active")
        namespace = Namespace.objects.get(name="Global")
        Prefix.objects.create(prefix="10.60.0.0/24", namespace=namespace, type="Pool", status=status)
        Prefix.objects.create(prefix="2001:db8:d0::/64", namespace=namespace, type="Pool", status=status)
        cls.ipv4_a = IPAddress.objects.create(address="10.60.0.1/32", namespace=namespace, status=status)
        cls.ipv4_b = IPAddress.objects.create(address="10.60.0.2/32", namespace=namespace, status=status)
        cls.ipv6_a = IPAddress.objects.create(address="2001:db8:d0::1/128", namespace=namespace, status=status)
        cls.ipv6_b = IPAddress.objects.create(address="2001:db8:d0::2/128", namespace=namespace, status=status)

    def setUp(self):
        """Reset zone serial before each test."""
        _reset_zone_serial(self.zone)

    def test_each_record_type_create_increments_serial(self):
        """Creating any DNS record type must increment the zone serial by exactly 1."""
        cases = [
            (NSRecord, {"name": "lc-c-ns", "server": "ns.lifecycle.example."}),
            (ARecord, {"name": "lc-c-a", "ip_address": self.ipv4_a}),
            (AAAARecord, {"name": "lc-c-aaaa", "ip_address": self.ipv6_a}),
            (CNAMERecord, {"name": "lc-c-cname", "alias": "target.lifecycle.example."}),
            (MXRecord, {"name": "lc-c-mx", "mail_server": "mail.lifecycle.example."}),
            (TXTRecord, {"name": "lc-c-txt", "text": "v=spf1 -all"}),
            (PTRRecord, {"name": "lc-c-ptr", "ptrdname": "host.lifecycle.example."}),
            (
                SRVRecord,
                {
                    "name": "_sip._tcp.lc-c",
                    "priority": 10,
                    "weight": 5,
                    "port": 5060,
                    "target": "sip.lifecycle.example.",
                },
            ),
        ]
        for model, kwargs in cases:
            with self.subTest(model=model.__name__):
                _reset_zone_serial(self.zone)
                model.objects.create(zone=self.zone, **kwargs)
                self.assertEqual(_refresh_serial(self.zone), 1)

    def test_each_materializing_record_field_update_increments_serial(self):
        """Changing a DNS-served field via full save() on each record type increments the serial."""
        cases = [
            (NSRecord, {"name": "lc-u-ns", "server": "ns1.lifecycle.example."}, "server", "ns2.lifecycle.example."),
            (ARecord, {"name": "lc-u-a", "ip_address": self.ipv4_a}, "ip_address", self.ipv4_b),
            (AAAARecord, {"name": "lc-u-aaaa", "ip_address": self.ipv6_a}, "ip_address", self.ipv6_b),
            (CNAMERecord, {"name": "lc-u-cname", "alias": "old.lifecycle.example."}, "alias", "new.lifecycle.example."),
            (
                MXRecord,
                {"name": "lc-u-mx", "mail_server": "mx1.lifecycle.example."},
                "mail_server",
                "mx2.lifecycle.example.",
            ),
            (TXTRecord, {"name": "lc-u-txt", "text": "original"}, "text", "updated"),
            (
                PTRRecord,
                {"name": "lc-u-ptr", "ptrdname": "host1.lifecycle.example."},
                "ptrdname",
                "host2.lifecycle.example.",
            ),
            (
                SRVRecord,
                {
                    "name": "_sip._tcp.lc-u",
                    "priority": 10,
                    "weight": 5,
                    "port": 5060,
                    "target": "sip1.lifecycle.example.",
                },
                "target",
                "sip2.lifecycle.example.",
            ),
        ]
        for model, create_kwargs, field, value in cases:
            with self.subTest(model=model.__name__, field=field):
                rec = _create_record_without_bump(model, zone=self.zone, **create_kwargs)
                _reset_zone_serial(self.zone)
                setattr(rec, field, value)
                rec.save()
                self.assertEqual(_refresh_serial(self.zone), 1)

    def test_mx_preference_and_srv_numeric_fields_increment_via_update_fields(self):
        """MX preference and SRV priority/weight/port increment the serial via save(update_fields=[field]).

        These fields are in _soa_materializing_fields(); this test exercises the update_fields
        filtering path in DNSRecord.save() that those subclass-specific memberships protect.
        """
        cases = [
            (
                MXRecord,
                {"name": "lc-u-mx-pref", "mail_server": "mx.lifecycle.example.", "preference": 10},
                "preference",
                20,
            ),
            (
                SRVRecord,
                {
                    "name": "_http._tcp.lc-u",
                    "priority": 10,
                    "weight": 5,
                    "port": 80,
                    "target": "web.lifecycle.example.",
                },
                "priority",
                20,
            ),
            (
                SRVRecord,
                {
                    "name": "_https._tcp.lc-u",
                    "priority": 10,
                    "weight": 5,
                    "port": 443,
                    "target": "web.lifecycle.example.",
                },
                "weight",
                10,
            ),
            (
                SRVRecord,
                {"name": "_ftp._tcp.lc-u", "priority": 10, "weight": 5, "port": 21, "target": "ftp.lifecycle.example."},
                "port",
                22,
            ),
        ]
        for model, create_kwargs, field, value in cases:
            with self.subTest(model=model.__name__, field=field):
                rec = _create_record_without_bump(model, zone=self.zone, **create_kwargs)
                _reset_zone_serial(self.zone)
                setattr(rec, field, value)
                rec.save(update_fields=[field])
                self.assertEqual(_refresh_serial(self.zone), 1)

    def test_each_record_type_delete_increments_serial(self):
        """Deleting any DNS record type must increment the zone serial by exactly 1."""
        cases = [
            (NSRecord, {"name": "lc-d-ns", "server": "ns.del.lifecycle.example."}),
            (ARecord, {"name": "lc-d-a", "ip_address": self.ipv4_a}),
            (AAAARecord, {"name": "lc-d-aaaa", "ip_address": self.ipv6_a}),
            (CNAMERecord, {"name": "lc-d-cname", "alias": "gone.lifecycle.example."}),
            (MXRecord, {"name": "lc-d-mx", "mail_server": "mail.del.lifecycle.example."}),
            (TXTRecord, {"name": "lc-d-txt", "text": "to-delete"}),
            (PTRRecord, {"name": "lc-d-ptr", "ptrdname": "del.lifecycle.example."}),
            (
                SRVRecord,
                {
                    "name": "_sip._tcp.lc-d",
                    "priority": 10,
                    "weight": 5,
                    "port": 5060,
                    "target": "sip.del.lifecycle.example.",
                },
            ),
        ]
        for model, kwargs in cases:
            with self.subTest(model=model.__name__):
                rec = _create_record_without_bump(model, zone=self.zone, **kwargs)
                _reset_zone_serial(self.zone)
                rec.delete()
                self.assertEqual(_refresh_serial(self.zone), 1)


# ── zone-level field tests ─────────────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialZoneFieldTestCase(TestCase):
    """Zone-level watched-field, create policy, and rollover tests."""

    @classmethod
    def setUpTestData(cls):
        """Create a zone for watched-field and policy tests."""
        cls.zone = _create_zone("zone-field.example")

    def setUp(self):
        """Reset serial and refresh zone to current DB state."""
        _reset_zone_serial(self.zone)

    def _assert_field_increments(self, field, new_value):
        """Assert that saving zone with a changed watched field bumps serial by 1."""
        _reset_zone_serial(self.zone)
        setattr(self.zone, field, new_value)
        self.zone.save()
        self.assertEqual(
            _refresh_serial(self.zone),
            1,
            f"Expected serial increment after changing '{field}'",
        )

    def test_watched_fields_trigger_increment(self):
        """Each watched zone field must bump the serial when changed."""
        watched = {
            "name": "zone-field-renamed.example",
            "enabled": False,
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
        """save(update_fields=[]) and save(update_fields=empty_generator) must not write or increment serial."""
        variants = [
            ("list", []),
            ("generator", (f for f in [])),
        ]
        for kind, empty_fields in variants:
            with self.subTest(kind=kind):
                _reset_zone_serial(self.zone)
                self.zone.soa_refresh = 99999  # change in memory but do not persist
                self.zone.save(update_fields=empty_fields)
                self.assertEqual(_refresh_serial(self.zone), 0, f"empty {kind} update_fields must not increment serial")
                self.zone.refresh_from_db()
                self.assertNotEqual(
                    self.zone.soa_refresh, 99999, f"empty {kind} update_fields must not persist field changes"
                )

    def test_user_submitted_serial_is_overridden_by_auto_increment(self):
        """A user-supplied soa_serial combined with a watched-field change is replaced by auto-increment."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=5)
        self.zone.refresh_from_db()

        self.zone.soa_serial = 999  # user-supplied value
        self.zone.soa_refresh = 43200  # watched field change triggers increment
        self.zone.save()

        # Serial must be 6 (5 + 1 via auto-increment), not 999
        self.assertEqual(_refresh_serial(self.zone), 6, "auto-increment must override the user-supplied serial")

    def test_stale_instance_cannot_overwrite_newer_serial(self):
        """A stale in-memory zone instance must never silently overwrite a newer DB serial.

        Also covers: full save without watched change locks to DB serial (rewind-bug guard).
        """
        # Sub-case A: description-only full save with DB serial at 7 and stale instance at 0.
        with self.subTest(case="unwatched_full_save_locks_to_db_serial"):
            DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=7)
            # self.zone still has soa_serial=0 from setUp (stale).
            self.zone.description = "only description changed"
            self.zone.save()
            self.assertEqual(_refresh_serial(self.zone), 7, "full save must not overwrite a newer DB serial")

        # Reset to stale state for next sub-case.
        _reset_zone_serial(self.zone)

        # Sub-case B: stale unwatched full save (DB at 5, stale at 0) → stays 5;
        # then stale watched save (DB at 8, stale at 5) → increments to 9.
        with self.subTest(case="stale_serial_increments_from_db_not_memory"):
            DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=5)
            self.zone.description = "description only"
            self.zone.save()
            self.assertEqual(_refresh_serial(self.zone), 5, "unwatched full save must not overwrite newer DB serial")

            self.zone.refresh_from_db()
            DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=8)
            # self.zone is now stale on soa_serial (5 in memory; 8 in DB).
            self.zone.soa_retry = 3600  # differs from default 7200 → detected as a change
            self.zone.save()
            self.assertEqual(
                _refresh_serial(self.zone), 9, "watched save must increment from DB serial (8), not stale (5)"
            )

        # Sub-case C: sentinel — internal increment path still works after the rewind fix.
        with self.subTest(case="internal_path_sentinel"):
            _reset_zone_serial(self.zone)
            TXTRecord.objects.create(name="sentinel-guard", text="data", zone=self.zone)
            self.assertEqual(
                _refresh_serial(self.zone), 1, "materializing change must advance serial via internal path"
            )

    def test_external_serial_write_raises_while_flag_on(self):
        """An external save(update_fields=['soa_serial']) that changes the serial raises ValidationError
        and leaves the DB serial unchanged."""
        self.zone.soa_serial = 42
        with self.assertRaises(ValidationError) as ctx:
            self.zone.save(update_fields=["soa_serial"])
        self.assertIn("soa_serial", ctx.exception.message_dict)
        self.assertEqual(_refresh_serial(self.zone), 0, "DB serial must remain unchanged after rejected write")

    def test_external_unchanged_serial_write_preserves_newer_db_serial(self):
        """A stale save(update_fields=['soa_serial']) strips soa_serial silently so a DB serial
        already advanced via the internal path is preserved, not rewound."""
        DNSZone.objects.get(pk=self.zone.pk).increment_soa_serial()
        # self.zone is now stale: soa_serial=0, _initial_soa_serial=0; DB serial=1.
        self.zone.save(update_fields=["soa_serial"])
        self.assertEqual(_refresh_serial(self.zone), 1, "stale unchanged serial write must not rewind the DB serial")

    def test_stale_unchanged_serial_mixed_with_other_fields_preserves_serial_persists_others(self):
        """A stale save(update_fields=['soa_serial', 'description']) strips the serial silently
        and persists the other named fields — the caller gets a partial update without a rewind."""
        DNSZone.objects.get(pk=self.zone.pk).increment_soa_serial()
        # self.zone is stale (soa_serial=0); advance description in memory.
        self.zone.description = "updated-by-stale-save"
        self.zone.save(update_fields=["soa_serial", "description"])
        self.assertEqual(_refresh_serial(self.zone), 1, "serial must not be rewound to stale value")
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.description, "updated-by-stale-save", "description must persist")

    @override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False)
    def test_external_serial_write_persists_when_flag_off(self):
        """With auto-increment disabled, save(update_fields=['soa_serial']) writes through normally."""
        self.zone.soa_serial = 42
        self.zone.save(update_fields=["soa_serial"])
        self.assertEqual(_refresh_serial(self.zone), 42, "manual serial write must persist when flag is off")

    def test_generator_update_fields_is_normalized_and_applied(self):
        """save(update_fields=generator) must not exhaust the generator before Django writes the field."""
        self.zone.soa_refresh = 43200
        self.zone.save(update_fields=(f for f in ["soa_refresh"]))

        self.zone.refresh_from_db()
        self.assertEqual(self.zone.soa_refresh, 43200, "generator update_fields must persist the field")
        self.assertEqual(
            _refresh_serial(self.zone), 1, "generator update_fields with watched field must increment serial"
        )

    @override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False)
    def test_no_increment_when_config_disabled(self):
        """No serial bump when SOA_SERIAL_AUTO_INCREMENT is off."""
        TXTRecord.objects.create(name="disabled-lc", text="nope", zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 0)

    def test_increment_only_affects_parent_zone(self):
        """Serial bump must not propagate to unrelated zones."""
        other = _create_zone("other-zone-lc.example", serial=100)
        TXTRecord.objects.create(name="iso-lc", text="isolated", zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 1)
        self.assertEqual(_refresh_serial(other), 100)

    # ── last_updated advancement ──────────────────────────────────────────────

    def test_automatic_bump_advances_last_updated(self):
        """An automatic serial increment must also advance the zone's last_updated timestamp."""
        before = DNSZone.objects.values_list("last_updated", flat=True).get(pk=self.zone.pk)
        TXTRecord.objects.create(name="lu-bump-lc", text="data", zone=self.zone)
        after = DNSZone.objects.values_list("last_updated", flat=True).get(pk=self.zone.pk)
        self.assertGreater(after, before, "last_updated must advance when soa_serial is bumped")
        self.assertEqual(_refresh_serial(self.zone), 1, "soa_serial must also advance")

    # ── zone create-time serial policy ───────────────────────────────────────

    def test_zone_create_serial_policy(self):
        """Managed-serial invariant on zone creation:
        non-default serial is rejected while managed; default (1) is accepted; any valid serial
        is accepted when auto-increment is disabled."""
        cases = [
            {"soa_serial": 999, "managed": True, "expect_raise": True, "zone_name": "policy-reject.example"},
            {"soa_serial": 1, "managed": True, "expect_raise": False, "zone_name": "policy-accept.example"},
            {"soa_serial": 999, "managed": False, "expect_raise": False, "zone_name": "policy-unmanaged.example"},
        ]
        for case in cases:
            label = f"serial={case['soa_serial']},managed={case['managed']}"
            with self.subTest(label=label):
                zone = DNSZone(
                    name=case["zone_name"],
                    filename=f"{case['zone_name']}.zone",
                    soa_mname=f"ns1.{case['zone_name']}",
                    soa_rname=f"admin@{case['zone_name']}",
                    soa_serial=case["soa_serial"],
                )
                if case["managed"]:
                    if case["expect_raise"]:
                        with self.assertRaises(ValidationError) as ctx:
                            zone.full_clean()
                        self.assertIn("soa_serial", ctx.exception.message_dict)
                    else:
                        zone.full_clean()  # must not raise
                else:
                    with override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False):
                        zone.full_clean()  # must not raise

    # ── rollover ─────────────────────────────────────────────────────────────

    def test_serial_rolls_over_to_one_at_uint32_max(self):
        """Serial at UINT32_MAX must roll over to 1 (RFC 2136 §7.11)."""
        zone = _create_zone("rollover-lc.example", serial=UINT32_MAX)
        TXTRecord.objects.create(name="roll-lc", text="trigger", zone=zone)
        self.assertEqual(_refresh_serial(zone), 1)


# ── deferred-field snapshot tests ──────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialDeferredFieldTestCase(TestCase):
    """Deferred soa_serial does not load eagerly or weaken serial-change rejection."""

    @classmethod
    def setUpTestData(cls):
        """Create a zone with a known non-default serial."""
        cls.zone = _create_zone("deferred.example", serial=42)

    def setUp(self):
        """Reset zone serial before each test."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=42)

    def test_queryset_projection_leaves_serial_deferred(self):
        """.only() and .defer() each leave soa_serial deferred rather than loading it."""
        with self.subTest(kind="only"):
            zone = DNSZone.objects.only("name").get(pk=self.zone.pk)
            self.assertIn(
                "soa_serial", zone.get_deferred_fields(), "a serial not selected via only() must stay deferred"
            )
        with self.subTest(kind="defer"):
            zone = DNSZone.objects.defer("soa_serial").get(pk=self.zone.pk)
            self.assertIn("soa_serial", zone.get_deferred_fields(), "soa_serial explicitly deferred must stay deferred")

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
            "deferred iteration must not issue an extra query per row",
        )

    def test_manual_serial_change_is_rejected_for_each_load_state(self):
        """A manual soa_serial change is rejected by validation regardless of how the instance was loaded."""
        # Deferred-read: read the deferred value then change it.
        with self.subTest(load_state="deferred_read"):
            zone = DNSZone.objects.defer("soa_serial").get(pk=self.zone.pk)
            self.assertEqual(zone.soa_serial, 42, "deferred read loads the real value")
            zone.soa_serial = 999
            with self.assertRaises(ValidationError):
                zone.clean()

        # Direct assignment to a still-deferred field.
        with self.subTest(load_state="direct_assign_to_deferred"):
            zone = DNSZone.objects.only("name").get(pk=self.zone.pk)
            self.assertIn("soa_serial", zone.get_deferred_fields())
            zone.soa_serial = 999
            self.assertNotIn("soa_serial", zone.get_deferred_fields())
            with self.assertRaises(ValidationError):
                zone.full_clean()

        # Fully-loaded instance.
        with self.subTest(load_state="full_load"):
            zone = DNSZone.objects.get(pk=self.zone.pk)
            zone.soa_serial = 43
            with self.assertRaises(ValidationError):
                zone.clean()

    def test_clean_with_unloaded_deferred_serial_does_not_force_load(self):
        """clean() must not load soa_serial merely because it remains deferred and unchanged."""
        zone = DNSZone.objects.only("name").get(pk=self.zone.pk)
        zone.clean()  # must not raise and must not force-load the field
        self.assertIn("soa_serial", zone.get_deferred_fields())

    def test_refresh_from_db_maintains_serial_snapshot(self):
        """refresh_from_db() correctly handles soa_serial for all combinations of fields= argument."""
        # Partial refresh excluding serial must preserve the in-memory value (and clean() still rejects it).
        with self.subTest(variant="partial_excluding_serial"):
            zone = DNSZone.objects.get(pk=self.zone.pk)
            zone.soa_serial = 999
            zone.refresh_from_db(fields=["name"])
            self.assertEqual(zone.soa_serial, 999, "partial refresh must not clobber an excluded field")
            with self.assertRaises(ValidationError):
                zone.clean()

        # Partial refresh including serial must reload it and clean() must accept the refreshed value.
        with self.subTest(variant="partial_including_serial"):
            zone = DNSZone.objects.get(pk=self.zone.pk)
            DNSZone.objects.filter(pk=zone.pk).update(soa_serial=77)
            zone.refresh_from_db(fields=["soa_serial"])
            self.assertEqual(zone.soa_serial, 77)
            zone.clean()  # must not raise: value matches what was just loaded

        # Full refresh must reload the DB value so clean() accepts it.
        with self.subTest(variant="full_refresh"):
            DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=42)  # reset after partial_including changed it
            zone = DNSZone.objects.get(pk=self.zone.pk)
            zone.soa_serial = 999
            zone.refresh_from_db()
            self.assertEqual(zone.soa_serial, 42)
            zone.clean()  # must not raise

        # Generator fields= including serial → serial reloaded; generator fields= excluding → not clobbered.
        with self.subTest(variant="generator_including_serial"):
            zone = DNSZone.objects.get(pk=self.zone.pk)
            DNSZone.objects.filter(pk=zone.pk).update(soa_serial=88)
            zone.refresh_from_db(fields=(f for f in ["soa_serial"]))
            self.assertEqual(zone.soa_serial, 88, "generator fields must still be inspected")
            zone.clean()  # must not raise

        with self.subTest(variant="generator_excluding_serial"):
            zone = DNSZone.objects.get(pk=self.zone.pk)
            zone.soa_serial = 999
            zone.refresh_from_db(fields=(f for f in ["name"]))
            self.assertEqual(zone.soa_serial, 999, "generator fields excluding serial must not clobber it")

    @override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False)
    def test_reused_instance_rejects_serial_reset_to_zero_after_save(self):
        """A reused instance cannot reset a persisted nonzero serial to zero."""
        legacy = _create_zone("reuse-legacy-zero.example", serial=0)
        zone = DNSZone.objects.get(pk=legacy.pk)

        zone.soa_serial = 1
        zone.validated_save()  # moving away from legacy 0 is allowed

        zone.soa_serial = 0
        with self.assertRaises(ValidationError):
            zone.validated_save()

        zone.refresh_from_db()
        self.assertEqual(zone.soa_serial, 1, "the persisted nonzero serial must be preserved")


# ── record mutation policy ─────────────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialRecordMutationTestCase(TestCase):
    """Test that a record save bumps the serial only when published DNS data changes.

    Also covers: F()-expression zone no-op move, zone-move with metadata, custom fields, tags,
    ModelForm, and common materializing fields (name/text/TTL/enabled).
    """

    @classmethod
    def setUpTestData(cls):
        """Create shared zone and an immutable TXT fixture for F()-expression tests."""
        cls.zone = _create_zone("record-metadata.example")
        with override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False):
            cls.f_expr_record = TXTRecord.objects.create(name="f-expr-r", text="payload", zone=cls.zone)

    def setUp(self):
        """Create a fresh mutable record with auto-increment disabled and reset the zone serial to 100."""
        with override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False):
            self.record = TXTRecord.objects.create(name="meta", text="payload", ttl=3600, zone=self.zone)
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=100)
        self.zone.refresh_from_db()

    def _form_data(self, **overrides):
        """Return a valid TXTRecordForm payload for the shared record."""
        data = {
            "name": self.record.name,
            "text": self.record.text,
            "_ttl": self.record.ttl,
            "enabled": self.record.enabled,
            "zone": self.zone,
            "description": self.record.description,
            "comment": self.record.comment,
        }
        data.update(overrides)
        return data

    def test_metadata_only_updates_do_not_increment(self):
        """description-only, comment-only, and both-together saves are metadata: no serial bump."""
        cases = [
            ("description", {"description": "internal note"}, ["description"]),
            ("comment", {"comment": "ticket JIRA-1234"}, ["comment"]),
            ("both", {"description": "note", "comment": "ticket"}, ["description", "comment"]),
        ]
        for label, attrs, update_fields in cases:
            with self.subTest(fields=label):
                DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=100)
                self.zone.refresh_from_db()
                for attr, val in attrs.items():
                    setattr(self.record, attr, val)
                self.record.save(update_fields=update_fields)
                self.assertEqual(_refresh_serial(self.zone), 100)

    def test_each_common_materializing_field_increments(self):
        """name, text, TTL, and enabled are each published DNS data: any change must increment the serial."""
        cases = [
            ("name", "name", "name-after"),
            ("text", "text", "changed"),
            ("ttl/_ttl", "_ttl", 900),
            ("enabled", "enabled", False),
        ]
        for label, update_field, value in cases:
            with self.subTest(field=label):
                DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=100)
                self.zone.refresh_from_db()
                # Use the real attribute name (ttl, not _ttl) for setattr.
                attr = "ttl" if update_field == "_ttl" else update_field
                setattr(self.record, attr, value)
                self.record.save(update_fields=[update_field])
                self.assertEqual(_refresh_serial(self.zone), 101)

    def test_generator_update_fields_is_normalized_and_increments(self):
        """A generator update_fields is normalized; a served-field change still increments."""
        self.record.text = "generated"
        self.record.save(update_fields=(field for field in ["text"]))
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_metadata_alongside_served_field_increments(self):
        """Metadata bundled with a served-data change still increments once."""
        self.record.comment = "ticket"
        self.record.text = "changed"
        self.record.save(update_fields=["comment", "text"])
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_bare_save_with_served_change_increments(self):
        """A full save (no update_fields) that changes served data increments."""
        self.record.text = "changed"
        self.record.save()
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_bare_save_noop_does_not_increment(self):
        """A full save that changes no published field does not increment (value diff)."""
        self.record.save()
        self.assertEqual(_refresh_serial(self.zone), 100)

    def test_no_op_expression_save_does_not_increment(self):
        """A no-op F() expression persists the same value, so the change diff must not bump.

        The comparison is against the post-save persisted value, not the in-memory F() object.
        """
        self.record.text = F("text")
        self.record.save(update_fields=["text"])
        self.record.refresh_from_db()
        self.assertEqual(self.record.text, "payload", "F('text') is a no-op; stored value is unchanged")
        self.assertEqual(_refresh_serial(self.zone), 100, "a persisted no-op must not advance the serial")

    def test_expression_save_that_changes_value_increments(self):
        """An F() expression that actually changes the persisted value increments."""
        self.record.text = F("comment")  # comment is empty; text becomes "" -> a real change
        self.record.save(update_fields=["text"])
        self.record.refresh_from_db()
        self.assertEqual(self.record.text, "", "F('comment') copied the empty comment into text")
        self.assertEqual(_refresh_serial(self.zone), 101)

    def test_custom_field_only_update_does_not_increment(self):
        """Custom-field data is metadata, not published DNS: a custom-field-only save must not increment."""
        custom_field = CustomField.objects.create(key="soa_env", label="SOA Env", type=CustomFieldTypeChoices.TYPE_TEXT)
        custom_field.content_types.add(ContentType.objects.get_for_model(TXTRecord))
        self.record.cf["soa_env"] = "prod"
        self.record.save()
        self.assertEqual(_refresh_serial(self.zone), 100)

    def test_tag_only_add_does_not_increment(self):
        """Adding tags is metadata management; the M2M change does not call save() and must not increment."""
        tag, _ = Tag.objects.get_or_create(name="soa-test-tag", defaults={"color": "9e9e9e"})
        self.record.tags.add(tag)
        self.assertIn(tag, self.record.tags.all(), "tag must be present to confirm the M2M mutation occurred")
        self.assertEqual(_refresh_serial(self.zone), 100)

    def test_modelform_metadata_only_does_not_increment(self):
        """The UI ModelForm path saves without update_fields; a metadata-only edit does not increment."""
        form = TXTRecordForm(
            data=self._form_data(description="form note", comment="form ticket"),
            instance=self.record,
        )
        self.assertTrue(form.is_valid(), f"form must be valid: {form.errors}")
        form.save()
        self.assertEqual(_refresh_serial(self.zone), 100)

    def test_modelform_served_field_change_increments(self):
        """The UI ModelForm path increments when a served field changes."""
        form = TXTRecordForm(
            data=self._form_data(text="form-changed"),
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

        self.record.zone = target
        self.record.comment = "moved"
        self.record.save(update_fields=["zone", "comment"])

        self.assertEqual(_refresh_serial(self.zone), 101)
        self.assertEqual(_refresh_serial(target), 201)

    def test_unpersisted_zone_assignment_with_metadata_only_does_not_increment(self):
        """Assigning a new zone but omitting it from update_fields leaves the record in place.

        Django does not persist the reassignment, and the only written field (comment) is metadata,
        so neither the original nor the target zone increments.
        """
        target = _create_zone("record-metadata-unpersisted.example")
        DNSZone.objects.filter(pk=target.pk).update(soa_serial=200)
        target.refresh_from_db()

        self.record.zone = target
        self.record.comment = "note only"
        self.record.save(update_fields=["comment"])  # zone deliberately omitted

        self.record.refresh_from_db()
        self.assertEqual(self.record.zone_id, self.zone.pk, "record must remain in its original zone")
        self.assertEqual(_refresh_serial(self.zone), 100, "a metadata-only save must not increment the persisted zone")
        self.assertEqual(_refresh_serial(target), 200, "the unpersisted target zone must not increment")

    def test_f_expression_zone_assignment_is_treated_as_no_move(self):
        """save(update_fields=['zone_id']) with an F('zone_id') expression is a no-op move:
        the persisted zone_id does not change so neither zone increments."""
        # f_expr_record was created with flag OFF so setUp serial reset to 100 is the baseline.
        self.f_expr_record.zone_id = F("zone_id")
        self.f_expr_record.save(update_fields=["zone_id"])
        self.f_expr_record.refresh_from_db()
        self.assertEqual(self.f_expr_record.zone_id, self.zone.pk, "record must remain in its original zone")
        self.assertEqual(_refresh_serial(self.zone), 100, "F() no-op zone assignment must not increment serial")


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
        """Pin the given zones to serial 600 and refresh."""
        pks = [zone.pk for zone in zones] or [self.zone.pk]
        DNSZone.objects.filter(pk__in=pks).update(soa_serial=600)
        for zone in zones or (self.zone,):
            zone.refresh_from_db()

    def setUp(self):
        """Reset zone serial before each test."""
        self._reset_serial()

    def test_queryset_delete_increments_serial(self):
        """QuerySet.delete() must increment the parent zone's serial."""
        _create_record_without_bump(TXTRecord, name="qs-del", text="x", zone=self.zone)
        TXTRecord.objects.filter(name="qs-del").delete()
        self.assertEqual(_refresh_serial(self.zone), 601)

    def test_queryset_delete_of_many_coalesces_to_one_increment(self):
        """Deleting N records in one QuerySet.delete() call must produce exactly one serial bump."""
        for index in range(5):
            _create_record_without_bump(TXTRecord, name=f"qs-multi-{index}", text="x", zone=self.zone)

        TXTRecord.objects.filter(name__startswith="qs-multi-").delete()
        self.assertEqual(_refresh_serial(self.zone), 601)

    def test_model_delete_increments_exactly_once(self):
        """Model.delete() increments the parent zone exactly once."""
        record = _create_record_without_bump(TXTRecord, name="single-del", text="x", zone=self.zone)
        record.delete()
        self.assertEqual(_refresh_serial(self.zone), 601)

    def test_queryset_delete_across_zones_increments_each(self):
        """A bulk delete spanning two zones increments both."""
        zone2 = _create_zone("bulk-delete-2.example")
        _create_record_without_bump(TXTRecord, name="span-1", text="x", zone=self.zone)
        _create_record_without_bump(TXTRecord, name="span-2", text="x", zone=zone2)
        self._reset_serial(self.zone, zone2)

        TXTRecord.objects.filter(name__in=["span-1", "span-2"]).delete()

        self.assertEqual(_refresh_serial(self.zone), 601)
        self.assertEqual(_refresh_serial(zone2), 601)

    def test_queryset_delete_does_not_increment_when_disabled(self):
        """With auto-increment disabled the serial is untouched."""
        with override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False):
            TXTRecord.objects.create(name="qs-off", text="x", zone=self.zone)
            TXTRecord.objects.filter(name="qs-off").delete()
        self.assertEqual(_refresh_serial(self.zone), 600)

    def test_cascade_delete_increments_serial(self):
        """A record removed by cascade from its IP address still increments."""
        _create_record_without_bump(ARecord, name="cascade-a", ip_address=self.ipv4, zone=self.zone)
        self._reset_serial()
        self.ipv4.delete()
        self.assertEqual(_refresh_serial(self.zone), 601)


# ── transaction coalescing tests ──────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialBulkCoalescingTestCase(TestCase):
    """Multiple DNS-materializing changes to the same zone in one transaction produce exactly one bump."""

    @classmethod
    def setUpTestData(cls):
        """Create shared zones for coalescing tests."""
        cls.zone = _create_zone("coalesce-test.example")
        cls.zone2 = _create_zone("coalesce-test2.example")

    def setUp(self):
        """Reset zone serials before each test."""
        DNSZone.objects.filter(pk__in=[self.zone.pk, self.zone2.pk]).update(soa_serial=0)
        self.zone.refresh_from_db()
        self.zone2.refresh_from_db()

    def test_multiple_creates_in_one_transaction_coalesce(self):
        """Three creates to one zone in a single atomic block produce one serial bump."""
        with transaction.atomic():
            TXTRecord.objects.create(name="coal-1", text="a", zone=self.zone)
            TXTRecord.objects.create(name="coal-2", text="b", zone=self.zone)
            TXTRecord.objects.create(name="coal-3", text="c", zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 1)

    def test_multi_zone_transaction_each_zone_bumps_once(self):
        """Changes to two zones in one transaction bump each zone exactly once."""
        with transaction.atomic():
            TXTRecord.objects.create(name="mz-1", text="a", zone=self.zone)
            TXTRecord.objects.create(name="mz-2", text="b", zone=self.zone2)
            TXTRecord.objects.create(name="mz-3", text="c", zone=self.zone)
        self.assertEqual(_refresh_serial(self.zone), 1)
        self.assertEqual(_refresh_serial(self.zone2), 1)

    def test_metadata_only_edit_does_not_bump_inside_atomic(self):
        """A metadata-only edit inside an atomic block must not bump the serial."""
        record = TXTRecord.objects.create(name="meta-coal", text="payload", zone=self.zone)
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=0)
        with transaction.atomic():
            record.description = "note"
            record.save(update_fields=["description"])
        self.assertEqual(_refresh_serial(self.zone), 0)

    def test_mixed_metadata_and_dns_edit_in_one_transaction_bumps_once(self):
        """A metadata edit and a DNS edit to the same zone in one atomic block bump exactly once."""
        record = TXTRecord.objects.create(name="mixed-coal", text="original", zone=self.zone)
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=0)
        with transaction.atomic():
            record.description = "note"
            record.save(update_fields=["description"])
            record.text = "changed"
            record.save(update_fields=["text"])
        self.assertEqual(_refresh_serial(self.zone), 1)


# ── savepoint coalescing isolation (TestCase) ─────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialSavepointCoalescingTestCase(TestCase):
    """Dirty-zone state first created inside a rolled-back savepoint must not suppress a later bump."""

    @classmethod
    def setUpTestData(cls):
        """Create shared zones."""
        cls.zone_b = _create_zone("sp-coal-b.example")
        cls.zone_c = _create_zone("sp-coal-c.example")

    def setUp(self):
        """Reset serials before each test."""
        DNSZone.objects.filter(pk__in=[self.zone_b.pk, self.zone_c.pk]).update(soa_serial=0)
        self.zone_b.refresh_from_db()
        self.zone_c.refresh_from_db()

    def test_first_dirty_op_in_rolled_back_savepoint_does_not_suppress_later_bump(self):
        """Zone B dirtied as the first op inside a rolled-back child must still bump when mutated after."""
        try:
            with transaction.atomic():  # rolled-back child — zone B is the FIRST dirty operation
                TXTRecord.objects.create(name="sp-b-rolled", text="b", zone=self.zone_b)
                raise RuntimeError("intentional savepoint rollback")
        except RuntimeError:
            pass
        TXTRecord.objects.create(name="sp-b-committed", text="b-ok", zone=self.zone_b)

        self.assertEqual(
            _refresh_serial(self.zone_b), 1, "zone B must bump once; rolled-back hook must not suppress it"
        )
        self.assertFalse(TXTRecord.objects.filter(name="sp-b-rolled").exists())
        self.assertTrue(TXTRecord.objects.filter(name="sp-b-committed").exists())

    def test_rolled_back_sibling_does_not_suppress_committed_sibling(self):
        """A rolled-back first child must not bleed its dirty state into the subsequent sibling."""
        try:
            with transaction.atomic():  # first child — zone B, rolled back
                TXTRecord.objects.create(name="sib-b", text="b", zone=self.zone_b)
                raise RuntimeError("rollback zone B")
        except RuntimeError:
            pass
        with transaction.atomic():  # second child — zone C, commits
            TXTRecord.objects.create(name="sib-c", text="c", zone=self.zone_c)

        self.assertEqual(_refresh_serial(self.zone_b), 0, "rolled-back zone must not bump")
        self.assertEqual(_refresh_serial(self.zone_c), 1, "sibling zone bumps once independently")


# ── on_commit liveness + rollback isolation (TransactionTestCase) ─────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialLivenessTestCase(TransactionTestCase):
    """Sequential committed transactions, autocommit liveness, and rollback isolation.

    Uses TransactionTestCase so each test gets a real flush; per-test setUp creates the zone.
    """

    def setUp(self):
        """Create zone with flag off so creation itself does not dirty state."""
        with override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False):
            self.zone = _create_zone("liveness.example")

    def test_two_sequential_committed_atomics_each_bump_independently(self):
        """Two back-to-back committed transactions each produce exactly one serial bump."""
        with transaction.atomic():
            TXTRecord.objects.create(name="reuse-1", text="first", zone=self.zone)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.soa_serial, 1)

        with transaction.atomic():
            TXTRecord.objects.create(name="reuse-2", text="second", zone=self.zone)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.soa_serial, 2)

    def test_standalone_creates_each_fire_on_commit_independently(self):
        """Each standalone record create commits its own atomic, fires on_commit, and bumps independently."""
        TXTRecord.objects.create(name="sa-1", text="a", zone=self.zone)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.soa_serial, 1, "first standalone create must bump to 1")

        TXTRecord.objects.create(name="sa-2", text="b", zone=self.zone)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.soa_serial, 2, "second standalone create must bump to 2 independently")

    def test_outer_rollback_drops_hook_so_next_transaction_bumps(self):
        """An outer rollback removes the on_commit hook; the next committed operation starts from fresh state."""
        try:
            with transaction.atomic():
                TXTRecord.objects.create(name="rb-lv", text="x", zone=self.zone)
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.soa_serial, 0, "rolled-back transaction must not advance serial")

        with transaction.atomic():
            TXTRecord.objects.create(name="rb-lv-2", text="y", zone=self.zone)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.soa_serial, 1, "committed transaction after rollback must bump to 1")

    def test_rollback_produces_no_increment(self):
        """A rolled-back transaction must not increment the serial; the subsequent transaction must."""
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
        self.assertEqual(self.zone.soa_serial, 1, "second transaction must increment")
        self.assertTrue(TXTRecord.objects.filter(name="rb-2").exists(), "rb-2 must be persisted")


# ── delete-in-savepoint tests (TestCase) ──────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialDeleteInSavepointTestCase(TestCase):
    """A delete rolled back inside a child savepoint must not miss, double-bump, or bleed into a later committed delete."""

    @classmethod
    def setUpTestData(cls):
        """Create a zone and records with auto-increment off so creation does not dirty state."""
        with override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False):
            cls.zone = _create_zone("del-sp.example")
            cls.record_a_pk = TXTRecord.objects.create(name="del-sp-a", text="a", zone=cls.zone).pk
            cls.record_b_pk = TXTRecord.objects.create(name="del-sp-b", text="b", zone=cls.zone).pk

    def setUp(self):
        """Reset serial before each test."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=0)
        self.zone.refresh_from_db()

    def test_delete_in_rolled_back_savepoint_then_committed_delete_bumps_once(self):
        """A rolled-back delete followed by a committed delete on the same record bumps exactly once."""
        try:
            with transaction.atomic():
                TXTRecord.objects.get(pk=self.record_a_pk).delete()
                raise RuntimeError("rollback inner savepoint")
        except RuntimeError:
            pass

        self.assertTrue(
            TXTRecord.objects.filter(pk=self.record_a_pk).exists(), "record must be restored after savepoint rollback"
        )
        self.assertEqual(_refresh_serial(self.zone), 0, "rolled-back delete must not advance serial")

        TXTRecord.objects.get(pk=self.record_a_pk).delete()
        self.assertEqual(_refresh_serial(self.zone), 1, "committed delete must advance serial exactly once")
        self.assertFalse(
            TXTRecord.objects.filter(pk=self.record_a_pk).exists(), "record must be gone after committed delete"
        )

    def test_mixed_create_and_delete_rolled_back_then_committed_create_bumps_once(self):
        """A rolled-back mixed create+delete leaves serial and records unchanged; a committed create after bumps once."""
        record_c_pk = _create_record_without_bump(TXTRecord, name="del-sp-c", text="c", zone=self.zone).pk

        try:
            with transaction.atomic():
                TXTRecord.objects.create(name="del-sp-rolled", text="x", zone=self.zone)
                TXTRecord.objects.get(pk=record_c_pk).delete()
                raise RuntimeError("rollback inner savepoint")
        except RuntimeError:
            pass

        self.assertFalse(TXTRecord.objects.filter(name="del-sp-rolled").exists(), "rolled-back create must not persist")
        self.assertTrue(
            TXTRecord.objects.filter(pk=record_c_pk).exists(), "record_c must be restored after savepoint rollback"
        )
        self.assertEqual(_refresh_serial(self.zone), 0, "rolled-back changes must not advance serial")

        TXTRecord.objects.create(name="del-sp-final", text="y", zone=self.zone)
        self.assertEqual(
            _refresh_serial(self.zone), 1, "committed create after rolled-back batch must bump exactly once"
        )


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
        """Create two zones for concurrency tests."""
        self.zone_a = _create_zone("concurrent-a.example")
        self.zone_b = _create_zone("concurrent-b.example")

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
        """Records moving between two zones in opposite directions must complete without deadlock."""
        rec_a = TXTRecord.objects.create(name="mover-a", text="a", zone=self.zone_a)
        rec_b = TXTRecord.objects.create(name="mover-b", text="b", zone=self.zone_b)
        DNSZone.objects.filter(pk__in=[self.zone_a.pk, self.zone_b.pk]).update(soa_serial=0)

        def _move(record, target_zone):
            def _fn():
                with transaction.atomic():
                    record.zone = target_zone
                    record.save()

            return _fn

        _run_concurrently(_move(rec_a, self.zone_b), _move(rec_b, self.zone_a))

        self.zone_a.refresh_from_db()
        self.zone_b.refresh_from_db()
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
        """Concurrent bulk-delete transactions spanning both zones must complete without deadlock (smoke test)."""
        first_a = TXTRecord.objects.create(name="deadlock-first-a", text="a", zone=self.zone_a)
        first_b = TXTRecord.objects.create(name="deadlock-first-b", text="b", zone=self.zone_b)
        second_b = TXTRecord.objects.create(name="deadlock-second-b", text="b", zone=self.zone_b)
        second_a = TXTRecord.objects.create(name="deadlock-second-a", text="a", zone=self.zone_a)
        DNSZone.objects.filter(pk__in=[self.zone_a.pk, self.zone_b.pk]).update(soa_serial=0)

        def _delete_ab():
            with transaction.atomic():
                TXTRecord.objects.filter(pk__in=[first_a.pk, first_b.pk]).delete()

        def _delete_ba():
            with transaction.atomic():
                TXTRecord.objects.filter(pk__in=[second_b.pk, second_a.pk]).delete()

        _run_concurrently(_delete_ab, _delete_ba)  # must complete without DeadlockDetected

        self.zone_a.refresh_from_db()
        self.zone_b.refresh_from_db()
        self.assertEqual(self.zone_a.soa_serial, 2)
        self.assertEqual(self.zone_b.soa_serial, 2)

    def test_concurrent_move_and_delete_increments_persisted_zone(self):
        """A record moved concurrently with its deletion increments only the zone it was actually deleted from."""
        record = TXTRecord.objects.create(name="move-delete", text="x", zone=self.zone_a)
        DNSZone.objects.filter(pk__in=[self.zone_a.pk, self.zone_b.pk]).update(soa_serial=0)

        def _move():
            with transaction.atomic():
                try:
                    r = TXTRecord.objects.select_for_update().get(pk=record.pk)
                    r.zone = self.zone_b
                    r.save()
                except TXTRecord.DoesNotExist:
                    pass  # delete won; mover tolerates the missing row

        def _delete():
            with transaction.atomic():
                TXTRecord.objects.filter(pk=record.pk).delete()

        _run_concurrently(_move, _delete)

        self.zone_a.refresh_from_db()
        self.zone_b.refresh_from_db()
        a, b = self.zone_a.soa_serial, self.zone_b.soa_serial
        self.assertIn(
            (a, b),
            [(1, 0), (1, 2)],
            f"expected delete-wins (1,0) or move-then-delete (1,2), got ({a},{b}); (2,1) would indicate a stale capture",
        )


# ── REST API serial increment tests ───────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialAPITestCase(APITestCase):
    """SOA-serial behavior via the REST API: create/update/delete/move/coalesce/rejection."""

    @classmethod
    def setUpTestData(cls):
        """Create the two zones used by the SOA REST tests."""
        cls.api_zone = _create_zone("api-serial-test.example")
        cls.api_zone2 = _create_zone("api-serial-test2.example")

    def setUp(self):
        """Reset zone serial before each test."""
        super().setUp()
        DNSZone.objects.filter(pk=self.api_zone.pk).update(soa_serial=0)
        DNSZone.objects.filter(pk=self.api_zone2.pk).update(soa_serial=0)
        self.api_zone.refresh_from_db()
        self.api_zone2.refresh_from_db()

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
        DNSZone.objects.filter(pk=self.api_zone.pk).update(soa_serial=0)

        url = reverse("plugins-api:nautobot_dns_models-api:txtrecord-detail", kwargs={"pk": record.pk})
        response = self.client.patch(url, data={"text": "after"}, format="json", **self.header)
        self.assertHttpStatus(response, http_status.HTTP_200_OK)
        self.assertEqual(_refresh_serial(self.api_zone), 1)

    def test_api_patch_move_increments_both_zones(self):
        """PATCHing a record's zone via the API is a move: both the old and new zones increment once."""
        self.add_permissions(
            "nautobot_dns_models.view_txtrecord",
            "nautobot_dns_models.change_txtrecord",
            "nautobot_dns_models.view_dnszone",
        )
        record = TXTRecord.objects.create(name="api-move", text="payload", zone=self.api_zone)
        DNSZone.objects.filter(pk=self.api_zone.pk).update(soa_serial=10)
        DNSZone.objects.filter(pk=self.api_zone2.pk).update(soa_serial=20)

        url = reverse("plugins-api:nautobot_dns_models-api:txtrecord-detail", kwargs={"pk": record.pk})
        response = self.client.patch(url, data={"zone": str(self.api_zone2.pk)}, format="json", **self.header)
        self.assertHttpStatus(response, http_status.HTTP_200_OK)
        self.assertEqual(_refresh_serial(self.api_zone), 11, "the old zone increments once")
        self.assertEqual(_refresh_serial(self.api_zone2), 21, "the new zone increments once")

    def test_api_patch_metadata_only_does_not_increment(self):
        """PATCHing only record note fields through DRF must not increment (metadata, not DNS data)."""
        self.add_permissions(
            "nautobot_dns_models.view_txtrecord",
            "nautobot_dns_models.change_txtrecord",
            "nautobot_dns_models.view_dnszone",
        )
        url_tpl = "plugins-api:nautobot_dns_models-api:txtrecord-detail"
        cases = [
            ("description", {"description": "just a description"}),
            ("comment", {"comment": "just a comment"}),
            ("both", {"description": "API note", "comment": "API ticket"}),
        ]
        for label, patch_data in cases:
            with self.subTest(fields=label):
                record = _create_record_without_bump(
                    TXTRecord, name=f"api-note-{label}", text="payload", zone=self.api_zone
                )
                DNSZone.objects.filter(pk=self.api_zone.pk).update(soa_serial=5)
                url = reverse(url_tpl, kwargs={"pk": record.pk})
                response = self.client.patch(url, data=patch_data, format="json", **self.header)
                self.assertHttpStatus(response, http_status.HTTP_200_OK)
                self.assertEqual(_refresh_serial(self.api_zone), 5)

    def test_api_delete_increments_serial(self):
        """DELETE a record via the API must increment the zone serial."""
        self.add_permissions(
            "nautobot_dns_models.view_txtrecord",
            "nautobot_dns_models.delete_txtrecord",
            "nautobot_dns_models.view_dnszone",
        )
        record = TXTRecord.objects.create(name="api-del", text="to-delete", zone=self.api_zone)
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
        self.assertEqual(_refresh_serial(self.api_zone), 1, "zone 1 must coalesce to one increment for its two records")
        self.assertEqual(_refresh_serial(self.api_zone2), 1, "zone 2 must coalesce to one increment for its one record")

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

    def test_api_zone_create_serial_policy(self):
        """POST to create a zone follows the managed-serial invariant:
        non-default serial is rejected while managed; default (1) is accepted; any valid serial
        is accepted when auto-increment is disabled."""
        self.add_permissions("nautobot_dns_models.add_dnszone", "nautobot_dns_models.view_dnsview")
        url = reverse("plugins-api:nautobot_dns_models-api:dnszone-list")
        cases = [
            {
                "label": "non_default_managed",
                "zone_name": "api-create-reject.example",
                "soa_serial": 999,
                "managed": True,
                "expect_status": http_status.HTTP_400_BAD_REQUEST,
            },
            {
                "label": "default_managed",
                "zone_name": "api-create-ok.example",
                "soa_serial": 1,
                "managed": True,
                "expect_status": http_status.HTTP_201_CREATED,
            },
            {
                "label": "unmanaged_any_serial",
                "zone_name": "api-create-unmanaged.example",
                "soa_serial": 999,
                "managed": False,
                "expect_status": http_status.HTTP_201_CREATED,
            },
        ]
        for case in cases:
            with self.subTest(label=case["label"]):
                data = {
                    "name": case["zone_name"],
                    "dns_view": str(self.api_zone.dns_view_id),
                    "filename": f"{case['zone_name']}.zone",
                    "soa_mname": f"ns1.{case['zone_name']}",
                    "soa_rname": f"admin@{case['zone_name']}",
                    "soa_serial": case["soa_serial"],
                }
                if case["managed"]:
                    response = self.client.post(url, data=data, format="json", **self.header)
                else:
                    with override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False):
                        response = self.client.post(url, data=data, format="json", **self.header)
                self.assertHttpStatus(response, case["expect_status"])
                if case["expect_status"] == http_status.HTTP_400_BAD_REQUEST:
                    self.assertIn("soa_serial", response.data)


# ── delete edge-case tests ─────────────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialDeleteEdgeCaseTestCase(TestCase):
    """Phantom delete, missing-row save, and mid-delete flag-flip edge cases."""

    def test_deleting_already_deleted_row_does_not_increment(self):
        """A zero-row delete (row already gone) must leave the serial unchanged."""
        zone = _create_zone("phantom.example", serial=0)
        record = TXTRecord.objects.create(name="r", text="a", zone=zone)
        DNSZone.objects.filter(pk=zone.pk).update(soa_serial=0)
        stale = TXTRecord.objects.get(pk=record.pk)
        record.delete()
        self.assertEqual(_refresh_serial(zone), 1, "first delete must increment")
        stale.delete()
        self.assertEqual(_refresh_serial(zone), 1, "zero-row delete of stale reference must not increment")

    def test_save_on_missing_row_does_not_raise_and_does_not_increment(self):
        """With the flag ON, saving a stale instance after its row is deleted must not raise DoesNotExist."""
        zone = _create_zone("missing-save.example", serial=0)
        record = TXTRecord.objects.create(name="missing-r", text="a", zone=zone)
        record_pk = record.pk  # preserve before delete clears record.pk
        DNSZone.objects.filter(pk=zone.pk).update(soa_serial=0)

        stale = TXTRecord.objects.get(pk=record_pk)
        record.delete()  # row gone; serial -> 1

        serial_after_delete = _refresh_serial(zone)

        # Must not raise DoesNotExist; must not advance the serial beyond the delete.
        stale.text = "updated"
        stale.save()  # Django INSERT-on-missing-row resurrects it; the feature must not change this

        self.assertEqual(
            _refresh_serial(zone),
            serial_after_delete,
            "saving a stale instance onto a missing row must not increment the serial",
        )
        resurrected = TXTRecord.objects.filter(pk=record_pk).first()
        self.assertIsNotNone(resurrected, "Django's native save() resurrects the row via INSERT")
        self.assertEqual(resurrected.text, "updated", "the resurrected row carries the new text value")

    def test_delete_still_increments_when_flag_disabled_after_pre_delete(self):
        """Disabling the flag between pre_delete and post_delete must not cancel the captured increment."""
        zone = _create_zone("delete-flag-decision.example", serial=100)
        record = TXTRecord.objects.create(name="flip", text="x", zone=zone)
        record_pk = record.pk  # preserve before delete clears record.pk
        DNSZone.objects.filter(pk=zone.pk).update(soa_serial=100)

        def _disable_flag_after_capture(sender, instance, **kwargs):  # pylint: disable=unused-argument
            constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT = False

        pre_delete.connect(_disable_flag_after_capture, sender=TXTRecord, dispatch_uid="soa-test-flip")
        try:
            record.delete()
        finally:
            pre_delete.disconnect(sender=TXTRecord, dispatch_uid="soa-test-flip")
            constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT = True

        self.assertFalse(TXTRecord.objects.filter(pk=record_pk).exists(), "the record must be deleted")
        self.assertEqual(
            _refresh_serial(zone), 101, "capture decided while enabled, so the delete still increments once"
        )


# ── form serial-rejection tests ────────────────────────────────────────────────


@override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=True)
class SOASerialFormPolicyTestCase(TestCase):
    """DNSZoneForm and DNSZoneBulkEditForm serial editability and ignore behavior."""

    @classmethod
    def setUpTestData(cls):
        """Create a zone fixture."""
        cls.zone = _create_zone("form-serial-test.example", serial=5)

    def setUp(self):
        """Reset zone serial before each test."""
        DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=5)
        self.zone.refresh_from_db()

    def _base_form_data(self, **overrides):
        """Return a minimal valid form payload for the test zone."""
        data = {
            "name": self.zone.name,
            "enabled": self.zone.enabled,
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

    def test_form_ignores_submitted_serial_while_managed(self):
        """Submitted soa_serial values (changed or blank) are silently ignored while auto-increment is on;
        the managed serial is preserved after save."""
        cases = [
            ("submitted_999", 999),
            ("submitted_blank", ""),
        ]
        for label, submitted_serial in cases:
            with self.subTest(label=label):
                # Reset serial to 5 before each sub-case (subTest shares setUp state).
                DNSZone.objects.filter(pk=self.zone.pk).update(soa_serial=5)
                self.zone.refresh_from_db()

                form = DNSZoneForm(data=self._base_form_data(soa_serial=submitted_serial), instance=self.zone)
                self.assertTrue(
                    form.fields["soa_serial"].disabled, "soa_serial must be disabled when auto-increment is on"
                )
                self.assertTrue(
                    form.is_valid(), f"disabled field ignores submission so form stays valid: {form.errors}"
                )
                self.assertEqual(
                    form.cleaned_data["soa_serial"],
                    5,
                    f"submitted {submitted_serial!r} must be ignored; serial stays 5",
                )
                form.save()
                self.zone.refresh_from_db()
                self.assertEqual(self.zone.soa_serial, 5, "saving must not wipe or change the managed serial")

    @override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False)
    def test_form_allows_serial_change_when_auto_increment_disabled(self):
        """DNSZoneForm must allow manual serial change when auto-increment is off."""
        form = DNSZoneForm(data=self._base_form_data(soa_serial=999), instance=self.zone)
        self.assertFalse(form.fields["soa_serial"].disabled, "soa_serial must be editable when auto-increment is off")
        self.assertTrue(form.is_valid(), f"form must allow serial change when auto-increment is off: {form.errors}")
        self.assertEqual(form.cleaned_data["soa_serial"], 999, "the submitted change must be accepted")

    def test_model_rejects_blank_serial_when_auto_increment_enabled(self):
        """The real guard: at the model level a blank/None soa_serial is rejected by validation."""
        self.zone.soa_serial = None
        with self.assertRaises(ValidationError) as ctx:
            self.zone.full_clean()
        self.assertIn("soa_serial", ctx.exception.message_dict)

    def test_form_create_ignores_submitted_serial_and_saves_default(self):
        """A new-zone DNSZoneForm with a submitted custom serial ignores it while managed,
        validates with the field default (1), and saves the zone with serial 1."""
        data = self._base_form_data(
            name="form-create-test.example",
            filename="form-create-test.example.zone",
            soa_serial=999,
        )
        form = DNSZoneForm(data=data)
        self.assertTrue(form.fields["soa_serial"].disabled, "soa_serial must be disabled on create when managed")
        self.assertTrue(form.is_valid(), f"form must be valid on create: {form.errors}")
        self.assertEqual(form.cleaned_data["soa_serial"], 1, "submitted 999 must be ignored; default 1 used")
        zone = form.save()
        zone.refresh_from_db()
        self.assertEqual(zone.soa_serial, 1, "persisted zone must have serial 1, not the submitted 999")

    def test_bulk_form_serial_editability_follows_config(self):
        """DNSZoneBulkEditForm disables soa_serial while auto-increment is on and enables it when off."""
        # Enabled: soa_serial is read-only and submitted change is ignored.
        with self.subTest(auto_increment="enabled"):
            form = DNSZoneBulkEditForm(DNSZone, data={"pk": [self.zone.pk], "soa_serial": 999})
            self.assertTrue(
                form.fields["soa_serial"].disabled,
                "soa_serial must be disabled in bulk edit when auto-increment is on",
            )
            self.assertTrue(form.is_valid(), f"disabled field ignores change; form stays valid: {form.errors}")
            self.assertIsNone(form.cleaned_data.get("soa_serial"), "the submitted 999 must be ignored in bulk edit")

        # Disabled: soa_serial is editable and the submitted value is accepted.
        with self.subTest(auto_increment="disabled"):
            with override_config(nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT=False):
                form = DNSZoneBulkEditForm(DNSZone, data={"pk": [self.zone.pk], "soa_serial": 999})
                self.assertFalse(
                    form.fields["soa_serial"].disabled,
                    "soa_serial must be editable in bulk edit when auto-increment is off",
                )
                self.assertTrue(
                    form.is_valid(), f"form must allow serial change when auto-increment is off: {form.errors}"
                )
                self.assertEqual(form.cleaned_data["soa_serial"], 999, "the submitted change must be accepted")
