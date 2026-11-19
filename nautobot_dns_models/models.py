"""Models for Nautobot DNS Models."""

from constance import config as constance_config
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from nautobot.apps.models import BaseModel, PrimaryModel, extras_features
from nautobot.core.models.fields import ForeignKeyWithAutoRelatedName
from nautobot.ipam.choices import IPAddressVersionChoices

from nautobot_dns_models.utils import (
    get_transaction_scoped_state,
    register_transaction_marker,
    transaction_marker_is_pending,
)

# Connection attribute holding the per-transaction {zone_pk: (serial, marker)} coalescing map.
# A zone bumped once in a transaction is not bumped again while its marker is pending, so any
# number of changes to one zone in one transaction advance its serial by one. The map covers the
# whole transaction rather than one savepoint, so bumps also coalesce across released savepoints.
_SOA_SERIAL_DIRTY_ATTR = "_soa_serial_dirty_zones"


def _get_dirty_zones():
    """Return the connection-bound, transaction-scoped ``{zone_pk: (serial, marker)}`` coalescing map."""
    return get_transaction_scoped_state(_SOA_SERIAL_DIRTY_ATTR, dict)["payload"]


# Largest serial the `soa_serial` column can hold. The column is a signed 32-bit integer, so the upper half
# of the RFC 1982 serial space cannot be stored.
SOA_SERIAL_MAX = 2147483647

SOA_SERIAL_MANAGED_MESSAGE = (
    "The SOA serial is managed automatically when auto-increment is enabled. "
    "Disable auto-increment to set the serial manually."
)
# Fields written by the internal serial bump. last_updated is carried so the zone's change-logged
# timestamp advances with the serial (auto_now only updates fields named in update_fields).
_SOA_SERIAL_INTERNAL_UPDATE_FIELDS = frozenset({"soa_serial", "last_updated"})


def dns_wire_label_length(label):
    """Return the wire-format (IDNA/Punycode) length of a DNS label."""
    if label.isascii():
        return len(label)

    return len("xn--" + label.encode("punycode").decode("ascii"))


class DNSModel(PrimaryModel):
    """Abstract Model for Nautobot DNS Models."""

    #
    # name is effectively a NOOP here; it's overridden in both subclasses but
    # is here so that linters don't complain about it being used in clean().
    name = models.CharField(max_length=200)
    ttl = models.IntegerField(
        validators=[MinValueValidator(300), MaxValueValidator(2147483647)], default=3600, help_text="Time To Live."
    )

    class Meta:
        """Meta class."""

        abstract = True

    def __str__(self):
        """Stringify instance."""
        return self.name  # pylint: disable=no-member

    @staticmethod
    def _validate_dns_label(label, field="name"):
        """
        Validate a DNS label for wire-format length using punycode encoding.

        Only checks for non-empty and length.
        """
        if not label:
            raise ValidationError({field: "Empty labels are not allowed"})
        length = dns_wire_label_length(label)
        if length > 63:
            raise ValidationError(
                {field: f"Label '{label}' exceeds the maximum length of 63 bytes (octets) in wire format."}
            )
        return length

    def clean(self):
        """
        Validate DNS label length and format per RFC 1035 §3.1 using punycode for wire-format length.

        Ensures each label in the name is ≤ 63 bytes (octets) in wire format and not empty.
        """
        super().clean()

        validation_level = getattr(constance_config, "nautobot_dns_models__DNS_VALIDATION_LEVEL")
        if validation_level == "wire-format":
            label_list = self.name.split(".")
            for label in label_list:
                self._validate_dns_label(label, field="name")


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "graphql",
    "relationships",
    "webhooks",
)
class DNSView(PrimaryModel):
    """Model for DNS Views."""

    name = models.CharField(max_length=200, help_text="Name of the View.", unique=True)
    description = models.TextField(help_text="Description of the View.", blank=True)
    prefixes = models.ManyToManyField(
        to="ipam.Prefix",
        related_name="dns_views",
        through="DNSViewPrefixAssignment",
        through_fields=("dns_view", "prefix"),
        blank=True,
        help_text="IP Prefixes that define the View.",
    )

    class Meta:
        """Meta attributes for DNSView."""

        verbose_name = "DNS View"
        verbose_name_plural = "DNS Views"

    def __str__(self):
        """Stringify instance."""
        return self.name


@extras_features("graphql")
class DNSViewPrefixAssignment(BaseModel):
    """Through model for DNSView and Prefix many-to-many relationship."""

    dns_view = ForeignKeyWithAutoRelatedName(
        DNSView,
        on_delete=models.CASCADE,
    )
    prefix = ForeignKeyWithAutoRelatedName(to="ipam.Prefix", on_delete=models.CASCADE)

    class Meta:
        """Meta attributes for DNSViewPrefixAssignment."""

        unique_together = [["dns_view", "prefix"]]
        verbose_name = "DNS View Prefix Assignment"
        verbose_name_plural = "DNS View Prefix Assignments"

    def __str__(self):
        """Stringify instance."""
        return f"{self.dns_view}: {self.prefix}"


def get_default_view_pk():
    """Return the default DNSView ID, creating it if necessary."""
    default_view, _ = DNSView.objects.get_or_create(
        name="Default", defaults={"description": "Default DNS view. Created by Nautobot DNS Models app."}
    )
    return default_view.pk


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "graphql",
    "relationships",
    "webhooks",
)
class DNSZone(DNSModel):
    """Model for DNS SOA Records. An SOA Record defines a DNS Zone."""

    # Serial as last loaded from the database; clean() compares against it to reject manual
    # changes. ``None`` means not loaded — a new instance, or a deferred serial never read.
    _initial_soa_serial = None

    @classmethod
    def from_db(cls, db, field_names, values):
        """Snapshot the loaded serial for clean(); a deferred serial is left unread and recorded as None."""
        instance = super().from_db(db, field_names, values)
        # pylint: disable-next=protected-access,no-member
        instance._initial_soa_serial = instance.soa_serial if "soa_serial" in field_names else None

        return instance

    # ``fields`` is named so the override can tell whether ``soa_serial`` was refreshed.
    # pylint: disable=keyword-arg-before-vararg
    def refresh_from_db(self, using=None, fields=None, *args, **kwargs):
        """Refresh from the database, and update the serial snapshot if ``soa_serial`` was refreshed."""
        if fields is not None:
            fields = list(fields)
        super().refresh_from_db(using, fields, *args, **kwargs)
        if fields is None or "soa_serial" in fields:
            self._initial_soa_serial = self.soa_serial

    name = models.CharField(max_length=200, help_text="FQDN of the Zone, w/ TLD. e.g example.com")
    dns_view = ForeignKeyWithAutoRelatedName(
        DNSView,
        on_delete=models.PROTECT,
        help_text="The DNS View this Zone belongs to.",
        verbose_name="View",
        default=get_default_view_pk,
    )
    ttl = models.IntegerField(
        validators=[MinValueValidator(300), MaxValueValidator(2147483647)],
        default=3600,
        help_text="Time To Live.",
        verbose_name="TTL",
    )
    filename = models.CharField(max_length=200, help_text="Filename of the Zone File.")
    description = models.TextField(help_text="Description of the Zone.", blank=True)
    soa_mname = models.CharField(
        max_length=200,
        help_text="FQDN of the Authoritative Name Server for Zone.",
        null=False,
        verbose_name="SOA MNAME",
    )
    soa_rname = models.EmailField(help_text="Admin Email for the Zone in the form", verbose_name="SOA RNAME")
    soa_refresh = models.IntegerField(
        validators=[MinValueValidator(300), MaxValueValidator(2147483647)],
        default=86400,
        help_text="Number of seconds after which secondary name servers should query the master for the SOA record, to detect zone changes.",
        verbose_name="SOA Refresh",
    )
    soa_retry = models.IntegerField(
        validators=[MinValueValidator(300), MaxValueValidator(2147483647)],
        default=7200,
        help_text="Number of seconds after which secondary name servers should retry to request the serial number from the master if the master does not respond.",
        verbose_name="SOA Retry",
    )
    soa_expire = models.IntegerField(
        validators=[MinValueValidator(300), MaxValueValidator(2147483647)],
        default=3600000,
        help_text="Number of seconds after which secondary name servers should stop answering request for this zone if the master does not respond. This value must be bigger than the sum of Refresh and Retry.",
        verbose_name="SOA Expire",
    )
    soa_serial = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(2147483647)],
        default=0,
        help_text="Serial number of the zone. This value must be incremented each time the zone is changed, and secondary DNS servers must be able to retrieve this value to check if the zone has been updated.",
        verbose_name="SOA Serial",
    )
    soa_minimum = models.IntegerField(
        validators=[MinValueValidator(300), MaxValueValidator(2147483647)],
        default=3600,
        help_text="Minimum TTL for records in this zone.",
        verbose_name="SOA Minimum",
    )

    class Meta:
        """Meta attributes for DNSZone."""

        unique_together = [["name", "dns_view"]]
        verbose_name = "DNS Zone"
        verbose_name_plural = "DNS Zones"

    # Attribute names of the fields that trigger a serial increment when changed on the zone itself.
    _SOA_SERIAL_WATCHED_FIELDS = frozenset(
        {
            "name",
            "dns_view_id",
            "ttl",
            "filename",
            "soa_mname",
            "soa_rname",
            "soa_refresh",
            "soa_retry",
            "soa_expire",
            "soa_minimum",
        }
    )

    # Set by bump_zone_serial on the instance it is about to save, so save() writes that serial through.
    _soa_serial_internal_bump = False

    @classmethod
    def bump_zone_serial(cls, zone_id):
        """Advance a zone's SOA serial by one, at most once per transaction, and return the serial.

        Returns None if the zone no longer exists. At ``SOA_SERIAL_MAX`` the serial wraps to 1.
        Callers check ``SOA_SERIAL_AUTO_INCREMENT`` and must be inside a transaction.
        """
        dirty = _get_dirty_zones()
        entry = dirty.get(zone_id)
        if entry is not None:
            # A pending marker means no savepoint rollback undid the earlier bump, so it still stands.
            serial, marker = entry
            if transaction_marker_is_pending(marker):
                return serial

            del dirty[zone_id]

        zone = cls.objects.select_for_update().filter(pk=zone_id).first()
        if zone is None:
            return None

        zone.soa_serial = 1 if zone.soa_serial >= SOA_SERIAL_MAX else zone.soa_serial + 1
        zone._soa_serial_internal_bump = True  # pylint: disable=protected-access
        zone.save(update_fields=_SOA_SERIAL_INTERNAL_UPDATE_FIELDS)
        # Registered at the savepoint nesting of the serial write, so rolling that write back drops the marker.
        marker = register_transaction_marker()
        dirty[zone_id] = (zone.soa_serial, marker)

        return zone.soa_serial

    def increment_soa_serial(self):
        """Advance this zone's SOA serial by one and update this instance to match."""
        with transaction.atomic(savepoint=False):
            new_serial = type(self).bump_zone_serial(self.pk)
        if new_serial is not None:
            self.soa_serial = new_serial
            self._initial_soa_serial = new_serial

    def save(self, *args, **kwargs):
        """Save, and bump the serial on zone self-changes.

        While auto-increment is on the serial is managed: a full save keeps the stored serial, and an
        ``update_fields`` save that names ``soa_serial`` raises if the serial was changed.
        """
        # Materialize update_fields once, so a generator is not exhausted before super().save() reads it.
        update_fields_set = kwargs.get("update_fields")
        if update_fields_set is not None:
            update_fields_set = frozenset(update_fields_set)
            kwargs["update_fields"] = update_fields_set

        internal_bump = self._soa_serial_internal_bump
        self._soa_serial_internal_bump = False

        if internal_bump and update_fields_set == _SOA_SERIAL_INTERNAL_UPDATE_FIELDS:
            super().save(*args, **kwargs)
            self._initial_soa_serial = self.soa_serial
            return

        if update_fields_set is not None and not update_fields_set:
            super().save(*args, **kwargs)
            return

        if not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT:
            super().save(*args, **kwargs)
            if update_fields_set is None or "soa_serial" in update_fields_set:
                self._initial_soa_serial = self.soa_serial
            return

        if not self.present_in_database:
            super().save(*args, **kwargs)
            self._initial_soa_serial = self.soa_serial
            return

        update_fields_set = self._managed_soa_serial_update_fields(update_fields_set)
        kwargs["update_fields"] = update_fields_set

        watched = self._SOA_SERIAL_WATCHED_FIELDS
        if update_fields_set is not None:
            written = update_fields_set
            if "dns_view" in written:
                written = written | {"dns_view_id"}
            watched = watched & written

        if not watched:
            super().save(*args, **kwargs)
            return

        with transaction.atomic():
            # Lock the row so the comparison, the save and the bump all see the same stored values.
            locked = DNSZone.objects.select_for_update().values(*watched, "soa_serial").get(pk=self.pk)
            should_increment = any(getattr(self, field) != locked[field] for field in watched)

            if should_increment or update_fields_set is None:
                self.soa_serial = locked["soa_serial"]

            super().save(*args, **kwargs)

            if should_increment:
                self.increment_soa_serial()
            elif update_fields_set is None:
                self._initial_soa_serial = self.soa_serial

    def _managed_soa_serial_update_fields(self, update_fields_set):
        """Return ``update_fields_set`` without ``soa_serial``, raising if the serial differs from its loaded value.

        An unchanged serial is dropped rather than written, because the in-memory value can be older than the
        stored one.
        """
        if update_fields_set is None or "soa_serial" not in update_fields_set:
            return update_fields_set

        if "soa_serial" not in self.get_deferred_fields():
            baseline = self._get_soa_serial_validation_baseline()
            if baseline is not None and self.soa_serial != baseline:
                raise ValidationError({"soa_serial": SOA_SERIAL_MANAGED_MESSAGE})

        return update_fields_set - {"soa_serial"}

    def _get_soa_serial_validation_baseline(self):
        """Return the serial as loaded from the database, or None for a new zone or a deferred serial."""
        if self._initial_soa_serial is not None:
            return self._initial_soa_serial

        if self.present_in_database and "soa_serial" not in self.get_deferred_fields():
            self._initial_soa_serial = DNSZone.objects.values_list("soa_serial", flat=True).get(pk=self.pk)
            return self._initial_soa_serial

        return None

    def clean(self):
        """Reject manual serial changes when auto-increment is on."""
        super().clean()

        serial_is_loaded = "soa_serial" not in self.get_deferred_fields()
        if not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT or not serial_is_loaded:
            return

        if self.present_in_database:
            # Compared with the serial as loaded, not the stored one, so an increment made by
            # another transaction since the load is not mistaken for a manual change.
            initial = self._get_soa_serial_validation_baseline()
            if initial is not None and self.soa_serial != initial:
                raise ValidationError({"soa_serial": SOA_SERIAL_MANAGED_MESSAGE})
        elif self.soa_serial != self._meta.get_field("soa_serial").default:
            # A new zone starts at the default. Onboard a zone at a specific serial with
            # auto-increment off.
            raise ValidationError({"soa_serial": SOA_SERIAL_MANAGED_MESSAGE})


class DNSRecord(DNSModel):
    """Primary Dns Record model for plugin."""

    name = models.CharField(max_length=200, help_text="FQDN of the Record, w/o TLD.")
    zone = ForeignKeyWithAutoRelatedName(DNSZone, on_delete=models.PROTECT)
    _ttl = models.IntegerField(
        validators=[MinValueValidator(300), MaxValueValidator(2147483647)],
        help_text="Time To Live (if no value is given, the Zone TTL will be used).",
        blank=True,
        null=True,
        verbose_name="TTL",
    )
    description = models.TextField(help_text="Description of the Record.", blank=True)
    comment = models.CharField(max_length=200, help_text="Comment for the Record.", blank=True)

    # Concrete record fields that are Nautobot metadata, not part of the published DNS record.
    # Changing only these must not advance the zone serial; "zone" is excluded because a zone
    # change is handled separately as a move.
    _SOA_SERIAL_NON_MATERIALIZING_FIELDS = frozenset(
        {"id", "created", "last_updated", "_custom_field_data", "description", "comment", "zone"}
    )

    @classmethod
    def _soa_materializing_fields(cls):
        """Return ``(name, attname)`` for each concrete field that is published in the zone."""
        return tuple(
            (field.name, field.attname)
            for field in cls._meta.concrete_fields
            if field.name not in cls._SOA_SERIAL_NON_MATERIALIZING_FIELDS
        )

    def _soa_locked_snapshot(self):
        """Lock this record's row and return its zone id and published values, or None if the row is gone."""
        attnames = [attname for _, attname in self._soa_materializing_fields()]
        return type(self).objects.select_for_update().values("zone_id", *attnames).filter(pk=self.pk).first()

    def _soa_zones_to_bump(self, update_fields_set, before):
        """Return the ids of the zones whose serial this save should advance, in primary key order.

        ``before`` is the locked pre-save row, or None for a create. The stored row is read back instead of
        using in-memory values, so an ``F()`` expression that stores the same value is not counted as a change.
        """
        fields = self._soa_materializing_fields()
        if before is not None and update_fields_set is not None:
            published = {"zone", "zone_id"}.union(*fields)
            if not published & update_fields_set:
                return []

        attnames = [attname for _, attname in fields]
        after = type(self).objects.values("zone_id", *attnames).filter(pk=self.pk).first()
        if after is None:
            return []

        if before is None:
            return [after["zone_id"]]

        if before["zone_id"] != after["zone_id"]:
            return sorted({before["zone_id"], after["zone_id"]}, key=str)

        if any(after[attname] != before[attname] for attname in attnames):
            return [after["zone_id"]]

        return []

    def save(self, *args, **kwargs):
        """Save, and bump the affected zones' SOA serials when the record's published DNS data changed.

        A create, a move to another zone, or a change to a published field bumps. An edit to the description,
        comment or custom fields does not. A full save of an instance whose row was deleted since it was loaded
        inserts the row again, and counts as a create.
        """
        # Materialize update_fields once, so a generator is not exhausted before super().save() reads it.
        update_fields_set = kwargs.get("update_fields")
        if update_fields_set is not None:
            update_fields_set = frozenset(update_fields_set)
            kwargs["update_fields"] = update_fields_set

        if (update_fields_set is not None and not update_fields_set) or (
            not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT
        ):
            super().save(*args, **kwargs)
            return

        with transaction.atomic():
            before = None if self._state.adding else self._soa_locked_snapshot()
            super().save(*args, **kwargs)

            for zone_id in self._soa_zones_to_bump(update_fields_set, before):
                DNSZone.bump_zone_serial(zone_id)

    # Deletion bumps through the pre_delete/post_delete receivers in signals.py, which also cover
    # QuerySet.delete(). A delete() override here would bump a second time.

    def clean(self):
        """
        Extend base validation to check total DNS name wire format length per RFC 1035 §3.1 using punycode for wire-format length.

        In addition to label checks, ensures the full DNS name (record + zone) does not exceed 255 bytes (octets) in wire format.
        """
        super().clean()

        if not hasattr(self, "zone"):
            raise ValidationError({"zone": "Zone is required"})

        validation_level = getattr(constance_config, "nautobot_dns_models__DNS_VALIDATION_LEVEL")
        if validation_level == "wire-format":
            record_label_list = self.name.split(".")
            zone_label_list = self.zone.name.split(".")

            wire_length = 0
            # Record labels
            for label in record_label_list:
                wire_length += 1 + dns_wire_label_length(label)
            # Zone labels
            for label in zone_label_list:
                wire_length += 1 + dns_wire_label_length(label)
            wire_length += 1  # Add the final zero byte for root

            if wire_length > 255:
                raise ValidationError(
                    {"name": "Total length of DNS name cannot exceed 255 bytes (octets) in wire format."}
                )

    class Meta:
        """Meta attributes for DnsRecord."""

        abstract = True

    @property
    def ttl(self):
        """Return the TTL value for the record."""
        if not self._ttl:
            return self.zone.ttl  # pylint: disable=no-member
        return self._ttl

    @ttl.setter
    def ttl(self, value):
        """Set the TTL value for the record."""
        self._ttl = value


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "relationships",
    "webhooks",
)
class NSRecord(DNSRecord):
    """NS Record model."""

    server = models.CharField(max_length=200, help_text="FQDN of an authoritative Name Server.")

    class Meta:
        """Meta attributes for NSRecord."""

        unique_together = [["name", "server", "zone"]]
        verbose_name = "NS Record"
        verbose_name_plural = "NS Records"


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "relationships",
    "webhooks",
)
class ARecord(DNSRecord):
    """A Record model."""

    address = models.ForeignKey(
        to="ipam.IPAddress",
        on_delete=models.CASCADE,
        limit_choices_to={"ip_version": IPAddressVersionChoices.VERSION_4},
        help_text="IP address for the record.",
    )

    class Meta:
        """Meta attributes for ARecord."""

        unique_together = [["name", "address", "zone"]]
        verbose_name = "A Record"
        verbose_name_plural = "A Records"

    def clean(self):
        """Validate that the referenced IP address is IPv4.

        Guard against dereferencing the relation when it's unset to avoid
        RelatedObjectDoesNotExist during form/model validation.
        """
        super().clean()
        if self.address_id is None:
            return
        if self.address.ip_version != IPAddressVersionChoices.VERSION_4:
            raise ValidationError({"address": "ARecord must reference an IPv4 address."})

    def save(self, *args, **kwargs):
        """Ensure model validation runs on direct ORM writes."""
        self.clean()
        return super().save(*args, **kwargs)


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "relationships",
    "webhooks",
)
class AAAARecord(DNSRecord):
    """AAAA Record model."""

    address = models.ForeignKey(
        to="ipam.IPAddress",
        on_delete=models.CASCADE,
        limit_choices_to={"ip_version": IPAddressVersionChoices.VERSION_6},
        help_text="IP address for the record.",
    )

    class Meta:
        """Meta attributes for AAAARecord."""

        unique_together = [["name", "address", "zone"]]
        verbose_name = "AAAA Record"
        verbose_name_plural = "AAAA Records"

    def clean(self):
        """Validate that the referenced IP address is IPv6.

        Guard against dereferencing the relation when it's unset to avoid
        RelatedObjectDoesNotExist during form/model validation.
        """
        super().clean()
        if self.address_id is None:
            return
        if self.address.ip_version != IPAddressVersionChoices.VERSION_6:
            raise ValidationError({"address": "AAAARecord must reference an IPv6 address."})

    def save(self, *args, **kwargs):
        """Ensure model validation runs on direct ORM writes."""
        self.clean()
        return super().save(*args, **kwargs)


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "relationships",
    "webhooks",
)
class CNAMERecord(DNSRecord):
    """CNAME Record model."""

    alias = models.CharField(max_length=200, help_text="FQDN of the Alias.")

    class Meta:
        """Meta attributes for CNAMERecord."""

        unique_together = [["name", "alias", "zone"]]
        verbose_name = "CNAME Record"
        verbose_name_plural = "CNAME Records"


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "relationships",
    "webhooks",
)
class MXRecord(DNSRecord):
    """MX Record model."""

    preference = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(65535)],
        default=10,
        help_text="Preference for the MX Record.",
    )
    mail_server = models.CharField(max_length=200, help_text="FQDN of the Mail Server.")

    class Meta:
        """Meta attributes for MXRecord."""

        unique_together = [["name", "mail_server", "zone"]]
        verbose_name = "MX Record"
        verbose_name_plural = "MX Records"


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "relationships",
    "webhooks",
)
class TXTRecord(DNSRecord):
    """TXT Record model."""

    text = models.CharField(max_length=256, help_text="Text for the TXT Record.")

    class Meta:
        """Meta attributes for TXTRecord."""

        unique_together = [["name", "text", "zone"]]
        verbose_name = "TXT Record"
        verbose_name_plural = "TXT Records"


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "relationships",
    "webhooks",
)
class PTRRecord(DNSRecord):
    """PTR Record model."""

    ptrdname = models.CharField(
        max_length=200, help_text="A domain name that points to some location in the domain name space."
    )

    class Meta:
        """Meta attributes for PTRRecord."""

        unique_together = [["name", "ptrdname", "zone"]]
        verbose_name = "PTR Record"
        verbose_name_plural = "PTR Records"

    def __str__(self):
        """String representation of PTRRecord."""
        return self.ptrdname


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "relationships",
    "webhooks",
)
class SRVRecord(DNSRecord):
    """SRV Record model."""

    priority = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(65535)],
        default=0,
        help_text="Priority of the SRV record.",
    )
    weight = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(65535)],
        default=0,
        help_text="Weight of the SRV record.",
    )
    port = models.IntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(65535)],
        help_text="Port number of the service.",
    )
    target = models.CharField(
        max_length=200,
        help_text="FQDN of the target host providing the service.",
    )

    class Meta:
        """Meta attributes for SRVRecord."""

        unique_together = [["name", "target", "port", "zone"]]
        verbose_name = "SRV Record"
        verbose_name_plural = "SRV Records"
