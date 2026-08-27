"""Models for Nautobot DNS Models."""

# pylint: disable=too-many-lines

from constance import config as constance_config
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, validate_email
from django.db import models, transaction
from nautobot.apps.models import BaseModel, PrimaryModel, extras_features
from nautobot.core.models.fields import ForeignKeyWithAutoRelatedName
from nautobot.extras.models import StatusField
from nautobot.ipam.choices import IPAddressVersionChoices
from netutils.ip import ipaddress_address

from nautobot_dns_models.utils import get_transaction_scoped_state

# Connection attribute holding the per-transaction {zone_pk: last_serial} coalescing map. Bumping
# a zone records its new serial here; a later bump of the same zone in the same transaction is a
# no-op, so N materializing changes to one zone in one transaction advance the serial by one.
_SOA_SERIAL_DIRTY_ATTR = "_soa_serial_dirty_zones"


def _get_dirty_zones():
    """Return the connection-bound, transaction-scoped ``{zone_pk: last_serial}`` coalescing map."""
    return get_transaction_scoped_state(_SOA_SERIAL_DIRTY_ATTR, dict)["payload"]


# Reverse-DNS roots per RFC 1035 §3.5 and RFC 3596 §2.5
RESERVED_ROOTS = {"in-addr.arpa", "ip6.arpa", "arpa"}

# All DNS integer fields use the full unsigned 32-bit range (0..4294967295).
# RFC 8767 §4 defines TTL as a 32-bit unsigned integer (updating RFC 2181).
# RFC 1035 §3.3.13 defines SOA fields as 32-bit values, explicitly unsigned for SERIAL and MINIMUM;
# RFC 1982 §7 specifies SERIAL's uint32 range and arithmetic.
UINT32_MAX = 2**32 - 1

# RFC 2136 §7.11: "A zone's SOA SERIAL should never be set to zero (0) due to interoperability
# problems with some older but widely installed implementations of DNS."
SOA_SERIAL_ZERO_MESSAGE = "RFC 2136 §7.11: a zone's SOA serial should not be set to 0. Use 1 or greater."
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


def _find_unescaped_dot(value):
    """Return the position of the first unescaped dot, or None."""
    character_is_escaped = False
    for index, character in enumerate(value):
        if character_is_escaped:
            character_is_escaped = False
            continue

        if character == "\\":
            character_is_escaped = True
        elif character == ".":
            return index

    return None


def normalize_soa_rname(value):
    """Normalize a basic DNS-style SOA RNAME mailbox to email form."""
    if not value or "@" in value:
        return value

    value_without_root = value.removesuffix(".")
    separator = _find_unescaped_dot(value_without_root)
    if separator is None:
        return value_without_root

    local_part = value_without_root[:separator].replace(r"\.", ".")
    domain = value_without_root[separator + 1 :]
    if not local_part or not domain or "\\" in local_part or "\\" in domain or "" in domain.split("."):
        return value

    return f"{local_part}@{domain}"


def prior_checks_ptr_record_creation(record):
    """Check if there is a matching reverse zone for this A/AAAA record's PTR record before creating it.

    Called from clean() so the failure surfaces before any DB write.
    """
    ptrdname = ipaddress_address(record.ip_address.host, "reverse_pointer")
    if DNSZone.find_reverse_zone_for_ptrdname(ptrdname, dns_view=record.zone.dns_view) is None:
        raise ValidationError(
            {
                "ip_address": (
                    f"Cannot auto-create PTR record: no matching reverse zone found "
                    f"in view '{record.zone.dns_view}' for {record.ip_address}."
                )
            }
        )


def create_auto_ptr_record(record):
    """Create a PTR record for the given A/AAAA record's IP address.

    Raises ValidationError if no matching reverse zone is found in the same DNS view.
    Skips creation silently if a PTR with the same owner name already exists in that reverse zone.
    """
    ptr_name = ipaddress_address(record.ip_address.host, "reverse_pointer")
    reverse_zone = DNSZone.find_reverse_zone_for_ptrdname(ptr_name, dns_view=record.zone.dns_view)
    if reverse_zone is None:
        raise ValidationError(
            {
                "ip_address": (
                    f"Cannot auto-create PTR record: no matching reverse zone found "
                    f"in view '{record.zone.dns_view}' for {record.ip_address}."
                )
            }
        )
    # RFC 1035 §3.5: PTR owner name is the reverse pointer, which is relative to the reverse zone.
    # As an example name is 20 and the whole fqdn `20.1.168.192.in-addr.arpa`
    relative_name = ptr_name.removesuffix(f".{reverse_zone.name}")
    # RFC 1035 §3.3.12: PTR RDATA (PTRDNAME) is the FQDN of the forward name.
    # Creating a PTR record for A record www in example.com, this should be `www.example.com`
    forward_fqdn = f"{record.name}.{record.zone.name}"
    if PTRRecord.objects.filter(name=relative_name, zone=reverse_zone).exists():
        return

    PTRRecord(name=relative_name, ptrdname=forward_fqdn, zone=reverse_zone).validated_save()


class DNSModel(PrimaryModel):
    """Abstract Model for Nautobot DNS Models."""

    #
    # name is effectively a NOOP here; it's overridden in both subclasses but
    # is here so that linters don't complain about it being used in clean().
    name = models.CharField(max_length=200)
    ttl = models.PositiveBigIntegerField(
        validators=[MaxValueValidator(UINT32_MAX)], default=3600, help_text="Time To Live."
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Whether this object is eligible for publication by external integrations.",
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
            # Allow apex (empty) names; otherwise validate each non-empty label.
            if self.name != "":
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


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "graphql",
    "relationships",
    "webhooks",
)
class DNSRegistrar(PrimaryModel):
    """Model for DNS Registrars."""

    name = models.CharField(max_length=200, help_text="Name of the Registrar.", unique=True)
    url = models.URLField(max_length=500, blank=True, help_text="Registrar URL.")
    account_number = models.CharField(max_length=100, blank=True, help_text="Registrar account number.")

    class Meta:
        """Meta attributes for DNSRegistrar."""

        verbose_name = "DNS Registrar"
        verbose_name_plural = "DNS Registrars"

    def __str__(self):
        """Stringify instance."""
        return self.name


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
        """Capture the DB-loaded serial so clean() can detect intentional changes.

        Snapshot only when ``soa_serial`` was selected.  Touching a deferred ``soa_serial`` here
        costs a query per instance (an N+1 on any ``.only()``/``.defer()`` queryset) and makes a
        later partial ``refresh_from_db(fields=[...])`` silently overwrite the caller's in-memory
        value, since loading it drops the field from Django's deferred set.  ``None`` means "never
        loaded"; ``clean()`` only fetches a comparison baseline if the field was later assigned or
        otherwise loaded.
        """
        instance = super().from_db(db, field_names, values)
        # super().from_db() is typed as the base class, so pylint misreads these attribute accesses.
        # pylint: disable-next=protected-access,no-member
        instance._initial_soa_serial = instance.soa_serial if "soa_serial" in field_names else None
        return instance

    # ``fields`` is named explicitly (with *args/**kwargs forwarding the rest) so the signature
    # stays compatible across Django 4.2/5.x and we can check whether ``soa_serial`` was refreshed.
    # pylint: disable=keyword-arg-before-vararg
    def refresh_from_db(self, using=None, fields=None, *args, **kwargs):
        """Update the captured serial, but only when ``soa_serial`` was actually refreshed."""
        # Normalize once: ``fields`` may be any iterable, and super() would consume a generator.
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
    ttl = models.PositiveBigIntegerField(
        validators=[MaxValueValidator(UINT32_MAX)],
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
    soa_rname = models.CharField(
        max_length=254,
        help_text="Mailbox of the person responsible for the zone or a single-label placeholder.",
        verbose_name="SOA RNAME",
    )
    soa_refresh = models.PositiveBigIntegerField(
        validators=[MaxValueValidator(UINT32_MAX)],
        default=86400,
        help_text="Number of seconds after which secondary name servers should query the master for the SOA record, to detect zone changes.",
        verbose_name="SOA Refresh",
    )
    soa_retry = models.PositiveBigIntegerField(
        validators=[MaxValueValidator(UINT32_MAX)],
        default=7200,
        help_text="Number of seconds after which secondary name servers should retry to request the serial number from the master if the master does not respond.",
        verbose_name="SOA Retry",
    )
    soa_expire = models.PositiveBigIntegerField(
        validators=[MaxValueValidator(UINT32_MAX)],
        default=3600000,
        help_text="Number of seconds after which secondary name servers should stop answering request for this zone if the master does not respond. This value must be bigger than the sum of Refresh and Retry.",
        verbose_name="SOA Expire",
    )
    soa_serial = models.PositiveBigIntegerField(
        validators=[MaxValueValidator(UINT32_MAX)],
        default=1,
        help_text=(
            "Serial number of the zone, incremented each time the zone changes so secondary DNS servers can "
            "detect updates. New zones default to 1; unchanged legacy zones with serial 0 remain editable."
        ),
        verbose_name="SOA Serial",
    )
    soa_minimum = models.PositiveBigIntegerField(
        validators=[MaxValueValidator(UINT32_MAX)],
        default=3600,
        help_text="Minimum TTL for records in this zone.",
        verbose_name="SOA Minimum",
    )

    tenant = models.ForeignKey(
        to="tenancy.Tenant",
        on_delete=models.PROTECT,
        related_name="dns_zones",
        blank=True,
        null=True,
    )
    auto_create_ptr = models.BooleanField(
        default=False,
        help_text="Automatically create PTR records when A/AAAA records are created in this zone.",
        verbose_name="Auto-create PTR Records",
    )

    # Fields that should trigger a serial increment when changed on the zone itself.
    _SOA_SERIAL_WATCHED_FIELDS = frozenset(
        {
            "name",
            "enabled",
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

    # One-shot sentinel: _bump_zone_serial sets it True on the instance it is about to save so save()
    # lets that internal soa_serial write through. The class-level default keeps it off every other
    # instance.
    _soa_serial_internal_bump = False

    @classmethod
    def _bump_zone_serial(cls, zone_id):
        """Advance one zone's SOA serial by one, at most once per transaction; return it or None.

        Per-transaction coalescing: all materializing changes to a zone funnel through here, and a
        connection-bound ``{zone_pk: last_serial}`` map records the bump, so a second call for the
        same zone in the same transaction is a no-op. A single ``select_for_update`` lookup does the
        existence check and the lock together, so a deleted zone is a no-op (returns None) rather
        than raising. The serial is an unsigned 32-bit counter (RFC 1982 §7); on overflow it wraps
        to 1 rather than 0, per RFC 2136 §7.11. Callers gate on ``SOA_SERIAL_AUTO_INCREMENT`` and
        must run inside a transaction.
        """
        dirty = _get_dirty_zones()
        if zone_id in dirty:
            # Already bumped this transaction; re-read to confirm the recorded serial survived, since
            # a nested savepoint may have rolled the increment back and it must then be redone. The
            # map is transaction-wide, not savepoint-keyed like the delete batch: coalescing is a
            # per-transaction guarantee and there is no savepoint-release hook to merge a nested bump
            # into the parent, so savepoint-keying would over-increment across released savepoints.
            # The value-equality check can only be fooled by a write that bypasses this map entirely
            # (QuerySet.update()/bulk_update()/raw SQL, all unsupported); the direct save() path is
            # covered by the internal-bump sentinel.
            current = cls.objects.values_list("soa_serial", flat=True).filter(pk=zone_id).first()
            if current is not None and current == dirty[zone_id]:
                return current
            dirty.pop(zone_id, None)

        zone = cls.objects.select_for_update().filter(pk=zone_id).first()
        if zone is None:
            return None
        zone.soa_serial = 1 if zone.soa_serial >= UINT32_MAX else zone.soa_serial + 1
        # Set the internal-bump sentinel so save() writes the serial through; an external
        # save(update_fields=["soa_serial"]) has none and is guarded. last_updated is included so the
        # automatic bump advances the zone's change-logged timestamp like any other edit.
        zone._soa_serial_internal_bump = True  # pylint: disable=protected-access
        zone.save(update_fields=_SOA_SERIAL_INTERNAL_UPDATE_FIELDS)
        dirty[zone_id] = zone.soa_serial
        return zone.soa_serial

    def increment_soa_serial(self):
        """Advance this zone's SOA serial by one and sync the in-memory snapshot.

        ``savepoint=False`` joins the caller's transaction without adding a per-call savepoint;
        when called standalone it still opens the outermost transaction the row lock needs.
        """
        with transaction.atomic(savepoint=False):
            new_serial = type(self)._bump_zone_serial(self.pk)
        if new_serial is not None:
            self.soa_serial = new_serial
            # Keep the intent snapshot in sync so clean() reflects the current state.
            self._initial_soa_serial = new_serial

    def save(self, *args, **kwargs):
        """Normalize the SOA RNAME, then trigger a serial increment on zone self-changes."""
        # Normalize the RNAME to email form before any persistence.
        self.soa_rname = normalize_soa_rname(self.soa_rname)

        # Normalize once: None means "all fields"; a non-None iterable lists explicit fields.
        # Consume any generator immediately so we can safely re-use the frozenset, and write
        # it back into kwargs so super().save() never receives the exhausted original.
        raw_update_fields = kwargs.get("update_fields")
        if raw_update_fields is None:
            update_fields_set = None
        else:
            update_fields_set = frozenset(raw_update_fields)
            kwargs["update_fields"] = update_fields_set

        # Consume the one-shot sentinel set by _bump_zone_serial. Read-and-clear up front so it can
        # never persist to a later save() on the same instance, whichever branch runs below.
        internal_bump = self._soa_serial_internal_bump
        self._soa_serial_internal_bump = False

        # Internal increment call from _bump_zone_serial: write the serial (+ last_updated) and skip
        # the re-entrant increment.
        if internal_bump and update_fields_set == _SOA_SERIAL_INTERNAL_UPDATE_FIELDS:
            super().save(*args, **kwargs)
            self._initial_soa_serial = self.soa_serial
            return

        if update_fields_set is not None and not update_fields_set:
            super().save(*args, **kwargs)
            return

        if not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT:
            super().save(*args, **kwargs)
            # Synchronize snapshot whenever soa_serial may have been written.
            if update_fields_set is None or "soa_serial" in update_fields_set:
                self._initial_soa_serial = self.soa_serial
            return

        if not self.present_in_database:
            super().save(*args, **kwargs)
            self._initial_soa_serial = self.soa_serial
            return

        # Auto-increment is on and the row exists. A full save (update_fields is None) resets
        # soa_serial to the DB value below, mirroring the disabled GUI field, so it never persists a
        # manual serial. An update_fields save that names soa_serial cannot be written directly here:
        # clean() (which rejects manual serial changes) is not called by Model.save(). The helper
        # rejects a changed serial and otherwise strips soa_serial from the write, so a stale
        # in-memory value cannot rewind a serial advanced since this instance was loaded. Other
        # named fields still persist. The internal bump is exempt via its sentinel, handled above.
        update_fields_set = self._managed_soa_serial_update_fields(update_fields_set)
        kwargs["update_fields"] = update_fields_set

        watched_in_update = (
            self._SOA_SERIAL_WATCHED_FIELDS & update_fields_set
            if update_fields_set is not None
            else self._SOA_SERIAL_WATCHED_FIELDS
        )

        if not watched_in_update:
            super().save(*args, **kwargs)
            # Synchronize snapshot when soa_serial was explicitly written.
            if update_fields_set is None or "soa_serial" in update_fields_set:
                self._initial_soa_serial = self.soa_serial
            return

        with transaction.atomic():
            # Lock the zone row before comparing to prevent TOCTOU races between
            # the watched-field comparison, the full save, and the serial increment.
            locked = DNSZone.objects.select_for_update().values(*watched_in_update, "soa_serial").get(pk=self.pk)
            should_increment = any(getattr(self, f) != locked[f] for f in watched_in_update)

            # Refresh soa_serial from the locked row when:
            # (a) a watched DNS field changed
            # (b) this is a full save (update_fields_set is None)
            if should_increment or update_fields_set is None:
                self.soa_serial = locked["soa_serial"]

            super().save(*args, **kwargs)

            if should_increment:
                self.increment_soa_serial()
            elif update_fields_set is None or "soa_serial" in update_fields_set:
                # No increment, but soa_serial was written.  Keeps snapshot in sync.
                self._initial_soa_serial = self.soa_serial

    def _managed_soa_serial_update_fields(self, update_fields_set):
        """Neutralize an external ``soa_serial`` write in ``update_fields`` while auto-increment is on.

        Only meaningful when the caller names ``soa_serial`` in ``update_fields``; a full save
        (``update_fields_set is None``) resets the serial to the DB value elsewhere in ``save()`` and
        so cannot persist a manual value. ``clean()`` enforces the managed-serial rule for the
        form/API paths, but ``Model.save()`` does not call ``clean()``, so a raw
        ``save(update_fields=["soa_serial"])`` reaches here directly. A serial that differs from the
        load-time snapshot is a deliberate manual change and is rejected. Otherwise ``soa_serial`` is
        stripped from the write and the returned field set omits it: writing an unchanged-but-stale
        value is not a no-op — it would rewind a serial advanced (e.g. by an increment) since this
        instance was loaded. Any other named fields still persist. The internal bump reaches
        ``super().save()`` before this via its sentinel and never calls this helper.
        """
        if update_fields_set is None or "soa_serial" not in update_fields_set:
            return update_fields_set
        if "soa_serial" not in self.get_deferred_fields():
            baseline = self._get_soa_serial_validation_baseline()
            if baseline is not None and self.soa_serial != baseline:
                raise ValidationError({"soa_serial": SOA_SERIAL_MANAGED_MESSAGE})
        return update_fields_set - {"soa_serial"}

    def _get_soa_serial_validation_baseline(self):
        """Return the DB-loaded serial used for validation, fetching only after deferred assignment."""
        if self._initial_soa_serial is not None:
            return self._initial_soa_serial
        if self.present_in_database and "soa_serial" not in self.get_deferred_fields():
            self._initial_soa_serial = DNSZone.objects.values_list("soa_serial", flat=True).get(pk=self.pk)
            return self._initial_soa_serial
        return None

    def clean(self):
        """Normalize/validate the SOA RNAME and reject manual serial changes when auto-increment is on."""
        super().clean()

        # Normalize and validate the SOA RNAME.
        invalid_rname_message = (
            "SOA RNAME must be a valid email address, a basic DNS-style mailbox with a fully qualified domain, "
            "or a single-label placeholder."
        )
        normalized_soa_rname = normalize_soa_rname(self.soa_rname)
        if "@" in normalized_soa_rname:
            try:
                validate_email(normalized_soa_rname)
            except ValidationError as exc:
                raise ValidationError({"soa_rname": invalid_rname_message}) from exc
        else:
            if not normalized_soa_rname or "." in normalized_soa_rname or "\\" in normalized_soa_rname:
                raise ValidationError({"soa_rname": invalid_rname_message})
            self._validate_dns_label(normalized_soa_rname, field="soa_rname")
        # Keep the in-memory instance canonical for callers that do not immediately save it.
        self.soa_rname = normalized_soa_rname

        # SOA serial policy.
        serial_is_loaded = "soa_serial" not in self.get_deferred_fields()
        initial = self._get_soa_serial_validation_baseline()

        if serial_is_loaded and self.soa_serial == 0 and (not self.present_in_database or initial != 0):
            raise ValidationError({"soa_serial": SOA_SERIAL_ZERO_MESSAGE})

        if constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT and serial_is_loaded:
            if self.present_in_database:
                # Existing zone: reject a change from the serial as loaded when this instance was
                # fetched (_initial_soa_serial set by from_db/refresh_from_db), not the current DB
                # value -- the latter would falsely reject when a concurrent increment lands between
                # form render and submission.
                if initial is not None and self.soa_serial != initial:
                    raise ValidationError({"soa_serial": SOA_SERIAL_MANAGED_MESSAGE})
            elif self.soa_serial != self._meta.get_field("soa_serial").default:
                # New zone: the serial is managed, so it must start at the default; a seeded value is
                # rejected. Disable auto-increment first to onboard a zone at a specific serial.
                raise ValidationError({"soa_serial": SOA_SERIAL_MANAGED_MESSAGE})

    class Meta:
        """Meta attributes for DNSZone."""

        unique_together = [["name", "dns_view"]]
        verbose_name = "DNS Zone"
        verbose_name_plural = "DNS Zones"

    def __str__(self):
        """Stringify instance."""
        return f"{self.name} ({self.dns_view})"

    @classmethod
    def find_reverse_zone_for_ptrdname(cls, ptrdname, dns_view=None):
        """Return the most-specific reverse DNSZone whose name matches a tail of `ptrdname`, otherwise None."""
        labels = ptrdname.split(".")
        for i in range(1, len(labels)):
            zone_name = ".".join(labels[i:])
            # We shouldn't match those cause are reserved to IANA
            if zone_name in RESERVED_ROOTS:
                break

            zones = cls.objects.filter(name=zone_name)
            if dns_view is not None:
                zones = zones.filter(dns_view=dns_view)
            zone = zones.first()
            if zone:
                return zone
        return None


@extras_features(
    "custom_fields",
    "custom_links",
    "custom_validators",
    "export_templates",
    "graphql",
    "relationships",
    "statuses",
    "webhooks",
)
class DNSRegistration(PrimaryModel):
    """Model representing the registration of a DNS zone with a registrar."""

    dns_registrar = ForeignKeyWithAutoRelatedName(
        DNSRegistrar,
        on_delete=models.PROTECT,
        help_text="Registrar used for this zone registration.",
        verbose_name="Registrar",
    )
    dns_zone = ForeignKeyWithAutoRelatedName(
        DNSZone,
        on_delete=models.PROTECT,
        help_text="Zone that is registered.",
        verbose_name="Zone",
    )
    status = StatusField(
        null=False,
        on_delete=models.PROTECT,
        help_text="Status of the DNS registration.",
        to="extras.status",
    )
    expiration_date = models.DateField(null=True, blank=True, help_text="Domain expiration date.")
    auto_renewal = models.BooleanField(default=False, help_text="Whether auto renewal is enabled.")
    registry_locked = models.BooleanField(default=False, help_text="Whether registry lock is enabled.")
    transfer_locked = models.BooleanField(default=False, help_text="Whether transfer lock is enabled.")
    privacy_enabled = models.BooleanField(default=False, help_text="Whether privacy protection is enabled.")
    website_forwarding_enabled = models.BooleanField(default=False, help_text="Whether website forwarding is enabled.")
    renewal_term_months = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(1200)],
        help_text="Renewal term in months.",
    )
    dnssec_enabled = models.BooleanField(
        default=False, help_text="Whether DNSSEC is enabled.", verbose_name="DNSSEC Enabled"
    )

    class Meta:
        """Meta attributes for DNSRegistration."""

        unique_together = [["dns_registrar", "dns_zone"]]
        verbose_name = "DNS Registration"
        verbose_name_plural = "DNS Registrations"

    def __str__(self):
        """Stringify instance."""
        return f"{self.dns_zone} @ {self.dns_registrar}"


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


class DNSRecord(DNSModel):
    """Primary Dns Record model for plugin."""

    name = models.CharField(max_length=200, help_text="FQDN of the Record, w/o TLD.")
    zone = ForeignKeyWithAutoRelatedName(DNSZone, on_delete=models.PROTECT)
    _ttl = models.PositiveBigIntegerField(
        validators=[MaxValueValidator(UINT32_MAX)],
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
        """Return ``(name, attname)`` for the concrete fields that materialize into published DNS.

        Derived from the model so each record subclass contributes its own data fields (record
        value, name, ttl, enabled) while metadata (description/comment/custom fields) and the zone
        (handled as a move) are excluded.
        """
        return tuple(
            (field.name, field.attname)
            for field in cls._meta.concrete_fields
            if field.name not in cls._SOA_SERIAL_NON_MATERIALIZING_FIELDS
        )

    def _soa_locked_snapshot(self):
        """Lock this record's row and return its ``zone_id`` plus materializing values, or None.

        ``filter().first()`` rather than ``get()``: if a concurrent transaction already deleted the
        row, return ``None`` so the save path stays missing-safe (matching the delete path) instead
        of raising ``DoesNotExist`` and adding a new failure mode to ``save()`` when the feature is on.
        """
        attnames = [attname for _, attname in self._soa_materializing_fields()]
        return type(self).objects.select_for_update().values("zone_id", *attnames).filter(pk=self.pk).first()

    def _soa_persisted_zone_id(self, update_fields_set, previous_zone_id):
        """Return the record's persisted zone id, respecting an ``update_fields`` that omits zone."""
        if update_fields_set is not None and "zone" not in update_fields_set and "zone_id" not in update_fields_set:
            # zone was not written, so the record kept its old zone.
            return previous_zone_id
        # Re-read the stored FK (not the in-memory value) so an F()/expression assignment resolves to
        # its persisted zone instead of being mistaken for a move.
        return type(self).objects.values_list("zone_id", flat=True).filter(pk=self.pk).first()

    def _soa_materializing_change(self, update_fields_set, before):
        """Return whether a materializing field's persisted value changed on this update.

        Compares the pre-save snapshot to a post-save re-read, so an ``F()``/expression assignment
        that persists the same value is correctly treated as a no-op rather than a DNS change.
        """
        if update_fields_set is not None:
            relevant = [
                attname
                for name, attname in self._soa_materializing_fields()
                if name in update_fields_set or attname in update_fields_set
            ]
        else:
            relevant = [attname for _, attname in self._soa_materializing_fields()]
        if not relevant:
            return False
        after = type(self).objects.filter(pk=self.pk).values(*relevant).first()
        if after is None:
            return False
        return any(after[attname] != before[attname] for attname in relevant)

    def _soa_zones_to_bump(self, update_fields_set, before, previous_zone_id, persisted_zone_id):
        """Return the zones (sorted, deduped) whose serial this save should advance."""
        is_move = previous_zone_id is not None and previous_zone_id != persisted_zone_id
        # A create (before is None) always materializes; a move changes both zones; a same-zone
        # update counts only when a materializing field actually changed.
        if before is not None and not is_move and not self._soa_materializing_change(update_fields_set, before):
            return ()
        zones = set()
        if persisted_zone_id:
            zones.add(persisted_zone_id)
        if is_move:
            zones.add(previous_zone_id)
        # Deterministic global order (by PK string) so opposing moves cannot deadlock.
        return sorted(zones, key=str)

    def save(self, *args, **kwargs):
        """Increment affected zones' SOA serials only when a record's published DNS data changes.

        A create, a move (zone change), or a change to a materializing field (name, ttl, enabled,
        or the record value) advances the serial. A metadata-only edit (description, comment, or
        custom fields) does not, since that data is never written into the zone served to secondary
        name servers, so a bump would trigger a needless zone transfer.
        """
        # Normalize update_fields once (consuming any generator) and write back into kwargs.
        raw_update_fields = kwargs.get("update_fields")
        if raw_update_fields is not None:
            kwargs["update_fields"] = frozenset(raw_update_fields)
        update_fields_set = kwargs.get("update_fields")

        if (update_fields_set is not None and not update_fields_set) or (
            not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT
        ):
            super().save(*args, **kwargs)
            return

        is_update = not self._state.adding
        with transaction.atomic():
            # Snapshot the pre-save row under a lock for move detection and the post-save
            # materializing-change comparison. None means create, or an update whose row a
            # concurrent transaction already deleted.
            before = self._soa_locked_snapshot() if is_update else None
            super().save(*args, **kwargs)

            if is_update and before is None:
                # The row was deleted mid-save; there is nothing published to bump. Let super()
                # save() proceed exactly as it does with the feature off, without raising.
                return

            previous_zone_id = before["zone_id"] if before is not None else None
            persisted_zone_id = self._soa_persisted_zone_id(update_fields_set, previous_zone_id)
            for zone_id in self._soa_zones_to_bump(update_fields_set, before, previous_zone_id, persisted_zone_id):
                DNSZone._bump_zone_serial(zone_id)  # pylint: disable=protected-access

    # Deletion is handled by pre_delete/post_delete receivers in signals.py rather than a
    # delete() override, so that QuerySet.delete() increments the serial too.
    # Overriding delete() as well would double-increment.

    def clean(self):
        """
        Extend base validation to check total DNS name wire format length per RFC 1035 §3.1 using punycode for wire-format length.

        In addition to label checks, ensures the full DNS name (record + zone) does not exceed 255 bytes (octets) in wire format.
        """
        # Normalize trailing-dot only when the record name is zone-qualified (e.g., "host.example.com.")
        if (
            isinstance(self.name, str)
            and self.name.endswith(".")
            and getattr(self, "zone", None)
            and getattr(self.zone, "name", None)
            and self.name.endswith(f"{self.zone.name}.")
        ):
            self.name = self.name[:-1]

        super().clean()

        if not hasattr(self, "zone"):
            raise ValidationError({"zone": "Zone is required"})

        self._validate_total_wire_length_if_enabled()
        self._enforce_cname_exclusivity_if_enabled()

    def _validate_total_wire_length_if_enabled(self) -> None:
        """Validate full DNS name (record + zone) total wire-format length if wire-format validation is enabled."""
        validation_level = getattr(constance_config, "nautobot_dns_models__DNS_VALIDATION_LEVEL")
        if validation_level != "wire-format":
            return

        record_label_list = [] if self.name == "" else self.name.split(".")
        zone_label_list = self.zone.name.split(".")

        wire_length = 0
        for label in record_label_list:
            wire_length += 1 + dns_wire_label_length(label)
        for label in zone_label_list:
            wire_length += 1 + dns_wire_label_length(label)
        wire_length += 1  # Final zero-length root label

        if wire_length > 255:
            raise ValidationError({"name": "Total length of DNS name cannot exceed 255 bytes (octets) in wire format."})

    def _enforce_cname_exclusivity_if_enabled(self) -> None:
        """Enforce mutual exclusivity between CNAME and other record types for exact (name, zone) matches."""
        enforce = getattr(constance_config, "nautobot_dns_models__CNAME_RESTRICTION_ENABLED", True)
        if not enforce or getattr(self, "name", None) is None or getattr(self, "zone_id", None) is None:
            return

        if isinstance(self, CNAMERecord):
            conflicting_models = (NSRecord, ARecord, AAAARecord, MXRecord, TXTRecord, PTRRecord, SRVRecord)
            for model in conflicting_models:
                if model.objects.filter(name=self.name, zone_id=self.zone_id).exists():
                    raise ValidationError(
                        {"name": "CNAME cannot co-exist with other records of the same name in this zone."}
                    )
        else:
            if CNAMERecord.objects.filter(name=self.name, zone_id=self.zone_id).exists():
                raise ValidationError({"name": "Record cannot co-exist with a CNAME of the same name in this zone."})

    class Meta:
        """Meta attributes for DnsRecord."""

        abstract = True

    @property
    def ttl(self):
        """Return the TTL value for the record."""
        if self._ttl is None:
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

    ip_address = models.ForeignKey(
        to="ipam.IPAddress",
        on_delete=models.CASCADE,
        limit_choices_to={"ip_version": IPAddressVersionChoices.VERSION_4},
        help_text="IP address for the record.",
        verbose_name="IP Address",
    )

    class Meta:
        """Meta attributes for ARecord."""

        unique_together = [["name", "ip_address", "zone"]]
        verbose_name = "A Record"
        verbose_name_plural = "A Records"

    def clean(self):
        """Validate that the referenced IP address is IPv4.

        Guard against dereferencing the relation when it's unset to avoid
        RelatedObjectDoesNotExist during form/model validation.
        """
        super().clean()
        if self.ip_address_id is None:
            return
        if self.ip_address.ip_version != IPAddressVersionChoices.VERSION_4:
            raise ValidationError({"ip_address": "ARecord must reference an IPv4 address."})
        if self._state.adding and self.zone.auto_create_ptr:  # pylint: disable=no-member
            prior_checks_ptr_record_creation(self)

    def save(self, *args, **kwargs):
        """Validate, save, and auto-create a PTR if the forward zone has auto_create_ptr enabled."""
        is_new = self._state.adding
        self.clean()
        super().save(*args, **kwargs)
        if is_new and self.zone.auto_create_ptr:  # pylint: disable=no-member
            create_auto_ptr_record(self)


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

    ip_address = models.ForeignKey(
        to="ipam.IPAddress",
        on_delete=models.CASCADE,
        limit_choices_to={"ip_version": IPAddressVersionChoices.VERSION_6},
        help_text="IP address for the record.",
        verbose_name="IP Address",
    )

    class Meta:
        """Meta attributes for AAAARecord."""

        unique_together = [["name", "ip_address", "zone"]]
        verbose_name = "AAAA Record"
        verbose_name_plural = "AAAA Records"

    def clean(self):
        """Validate that the referenced IP address is IPv6.

        Guard against dereferencing the relation when it's unset to avoid
        RelatedObjectDoesNotExist during form/model validation.
        """
        super().clean()
        if self.ip_address_id is None:
            return
        if self.ip_address.ip_version != IPAddressVersionChoices.VERSION_6:
            raise ValidationError({"ip_address": "AAAARecord must reference an IPv6 address."})
        if self._state.adding and self.zone.auto_create_ptr:  # pylint: disable=no-member
            prior_checks_ptr_record_creation(self)

    def save(self, *args, **kwargs):
        """Validate, save, and auto-create a PTR if the forward zone has auto_create_ptr enabled."""
        is_new = self._state.adding
        self.clean()
        super().save(*args, **kwargs)
        if is_new and self.zone.auto_create_ptr:  # pylint: disable=no-member
            create_auto_ptr_record(self)


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
