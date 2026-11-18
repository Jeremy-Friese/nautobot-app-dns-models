"""Models for Nautobot DNS Models."""
# pylint: disable=too-many-lines

import hashlib

from constance import config as constance_config
from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, validate_email
from django.db import models, transaction
from django.db.models.functions import Lower
from nautobot.apps.models import BaseModel, PrimaryModel, extras_features
from nautobot.core.models.fields import ForeignKeyWithAutoRelatedName
from nautobot.extras.models import StatusField
from nautobot.ipam.choices import IPAddressVersionChoices
from netutils.ip import ipaddress_address

from nautobot_dns_models.utils import (
    compute_zone_parents,
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


# Reverse-DNS roots per RFC 1035 §3.5 and RFC 3596 §2.5
RESERVED_ROOTS = {"in-addr.arpa", "ip6.arpa", "arpa"}

# All DNS integer fields use the full unsigned 32-bit range (0..4294967295).
# RFC 8767 §4 defines TTL as a 32-bit unsigned integer (updating RFC 2181).
# RFC 1035 §3.3.13 defines SOA fields as 32-bit values, explicitly unsigned for SERIAL and MINIMUM;
# RFC 1982 §7 specifies SERIAL's uint32 range and arithmetic.
UINT32_MAX = 2**32 - 1

# RFC 1035 §3.3 defines a <character-string> as a length octet followed by up to 255 octets.
TXT_CHARACTER_STRING_MAX_OCTETS = 255
# RFC 1035 §3.3.14, §4.1.3: TXT RDATA, including each length octet, has a limit of 16-bit RDLENGTH.
TXT_RDATA_MAX_OCTETS = 2**16 - 1

# RFC 1035 §2.3.3 and RFC 4343 §2: domain name comparisons are case-insensitive.
DNS_ZONE_NAME_UNIQUE_ERROR_CODE = "dns_zone_name_not_unique"

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


def _decode_txt_escapes(value, field="text"):
    r"""Decode a quoted <character-string> into wire octets: `\DDD` is one octet, `\X` is X (RFC 1035 §5.1)."""
    decoded = bytearray()
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            decoded.extend(character.encode("utf-8"))
            index += 1
            continue

        escape = value[index + 1 : index + 4]
        if len(escape) == 3 and escape.isdigit():
            octet = int(escape)
            if octet > 255:
                raise ValidationError({field: rf"Invalid escape '\{escape}': decimal escapes must be 0-255."})
            decoded.append(octet)
            index += 4
            continue

        if index + 1 >= len(value):
            raise ValidationError({field: "Invalid trailing backslash in quoted character-string."})
        decoded.extend(value[index + 1].encode("utf-8"))
        index += 2

    return bytes(decoded)


def _parse_quoted_txt_character_strings(value, field="text"):
    """Parse whitespace-separated, double-quoted <character-string> values per RFC 1035 §5.1."""
    character_string_list = []
    index = 0
    length = len(value)
    while index < length:
        if value[index].isspace():
            index += 1
            continue
        if value[index] != '"':
            raise ValidationError(
                {field: "Expected a double-quoted character-string; mixing quoted and unquoted text is not supported."}
            )

        index += 1
        start = index
        while index < length:
            if value[index] == "\\":
                index += 2
                continue
            if value[index] == '"':
                break
            index += 1

        if index >= length:
            raise ValidationError({field: "Unterminated quoted character-string."})

        character_string_list.append(_decode_txt_escapes(value[start:index], field=field))
        index += 1

    if not character_string_list:
        raise ValidationError({field: "No character-strings found."})

    return character_string_list


def _chunk_utf8_octets(value, max_octets):
    """Split a string into UTF-8 encoded chunks of at most `max_octets` bytes, never splitting a character."""
    chunk_list = []
    current = []
    current_octets = 0
    for character in value:
        character_octets = len(character.encode("utf-8"))
        if current_octets + character_octets > max_octets:
            chunk_list.append("".join(current))
            current = []
            current_octets = 0
        current.append(character)
        current_octets += character_octets

    if current:
        chunk_list.append("".join(current))

    return [chunk.encode("utf-8") for chunk in chunk_list]


def split_txt_character_strings(value, field="text"):
    """Return `value` as TXT <character-string> values, each as wire-format `bytes` (RFC 1035 §3.3.14).

    A value starting with a double quote is parsed as presentation format (RFC 1035 §5.1);
    any other value is split into 255-octet chunks.
    """
    if value is None:
        return []

    if value.lstrip().startswith('"'):
        return _parse_quoted_txt_character_strings(value.strip(), field=field)

    if value == "":
        return [b""]

    return _chunk_utf8_octets(value, TXT_CHARACTER_STRING_MAX_OCTETS)


def txt_wire_rdata_length(character_string_list):
    """Return the wire-format TXT RDATA length: each <character-string> plus its length octet."""
    return sum(1 + len(character_string) for character_string in character_string_list)


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
    portal_url = models.URLField(
        max_length=500, blank=True, help_text="Registrar portal URL.", verbose_name="Portal URL"
    )
    account_number = models.CharField(max_length=100, blank=True, help_text="Registrar account number.")

    class Meta:
        """Meta attributes for DNSRegistrar."""

        verbose_name = "DNS Registrar"
        verbose_name_plural = "DNS Registrars"

    def __str__(self):
        """Stringify instance."""
        return self.name


def get_default_view():
    """Return the default DNSView instance, creating it if necessary."""
    default_view, _ = DNSView.objects.get_or_create(
        name="Default", defaults={"description": "Default DNS view. Created by Nautobot DNS Models app."}
    )
    return default_view


def get_default_view_pk():
    """Return the default DNSView ID, creating it if necessary."""
    return get_default_view().pk


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
    parent = models.ForeignKey(
        to="self",
        on_delete=models.SET_NULL,
        related_name="children",
        blank=True,
        null=True,
        editable=False,
        help_text=("Closest enclosing zone (ancestor) in the same DNS View. Set automatically (RFC 9499)."),
        verbose_name="Parent Zone",
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

    # The case-insensitive UniqueConstraint has no `fields`, so Nautobot cannot derive the natural key from it.
    natural_key_field_names = ["name", "dns_view"]

    class Meta:
        """Meta attributes for DNSZone."""

        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                "dns_view",
                name="nautobot_dns_models_dnszone_name_ci_unique",
                violation_error_code=DNS_ZONE_NAME_UNIQUE_ERROR_CODE,
                violation_error_message="A DNS zone with this name (ignoring letter case) already exists in this view.",
            ),
        ]
        verbose_name = "DNS Zone"
        verbose_name_plural = "DNS Zones"

    def __str__(self):
        """Stringify instance."""
        return f"{self.name} ({self.dns_view})"

    # Attribute names of the fields that trigger a serial increment when changed on the zone itself.
    _SOA_SERIAL_WATCHED_FIELDS = frozenset(
        {
            "name",
            "dns_view_id",
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

    # Set by bump_zone_serial on the instance it is about to save, so save() writes that serial through.
    _soa_serial_internal_bump = False

    @classmethod
    def bump_zone_serial(cls, zone_id):
        """Advance a zone's SOA serial by one, at most once per transaction, and return the serial.

        Returns None if the zone no longer exists. On overflow the serial wraps to 1, not 0 (RFC 2136 §7.11).
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

        zone.soa_serial = 1 if zone.soa_serial >= UINT32_MAX else zone.soa_serial + 1
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
        """Normalize the SOA RNAME, save, keep zone parents current, and bump the serial on zone self-changes.

        While auto-increment is on the serial is managed: a full save keeps the stored serial, and an
        ``update_fields`` save that names ``soa_serial`` raises if the serial was changed.
        """
        self.soa_rname = normalize_soa_rname(self.soa_rname)

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
            self._save_and_rebuild_hierarchy(*args, **kwargs)
            return

        if not constance_config.nautobot_dns_models__SOA_SERIAL_AUTO_INCREMENT:
            self._save_and_rebuild_hierarchy(*args, **kwargs)
            if update_fields_set is None or "soa_serial" in update_fields_set:
                self._initial_soa_serial = self.soa_serial
            return

        if not self.present_in_database:
            self._save_and_rebuild_hierarchy(*args, **kwargs)
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
            self._save_and_rebuild_hierarchy(*args, **kwargs)
            return

        with transaction.atomic():
            # Lock the row so the comparison, the save and the bump all see the same stored values.
            locked = DNSZone.objects.select_for_update().values(*watched, "soa_serial").get(pk=self.pk)
            should_increment = any(getattr(self, field) != locked[field] for field in watched)

            if should_increment or update_fields_set is None:
                self.soa_serial = locked["soa_serial"]

            self._save_and_rebuild_hierarchy(*args, **kwargs)

            if should_increment:
                self.increment_soa_serial()
            elif update_fields_set is None:
                self._initial_soa_serial = self.soa_serial

    def _save_and_rebuild_hierarchy(self, *args, **kwargs):
        """Save, and recompute the parent of every zone in this zone's DNS View.

        A new, renamed or moved zone can change the parent of other zones, so the whole view is recomputed. If the
        zone moved to another view, its former view is recomputed too. A save that keeps the name and DNS View
        changes no parent, so it skips the recompute and keeps the stored parent.
        """
        former = DNSZone.objects.filter(pk=self.pk).values_list("name", "dns_view_id").first()

        if former == (self.name, self.dns_view_id):
            self.refresh_from_db(fields=["parent"])
            super().save(*args, **kwargs)
            return

        with transaction.atomic():
            super().save(*args, **kwargs)
            rebuild_zone_hierarchy(dns_view=self.dns_view_id)
            if former is not None and former[1] != self.dns_view_id:
                rebuild_zone_hierarchy(dns_view=former[1])

        self.refresh_from_db(fields=["parent"])

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

    def validate_constraints(self, exclude=None):
        """Report a zone name that collides with another zone in the same view as an error on `name`."""
        try:
            super().validate_constraints(exclude=exclude)
        except ValidationError as error:
            errors = error.update_error_dict({})
            for non_field_error in errors.pop(NON_FIELD_ERRORS, []):
                field = "name" if non_field_error.code == DNS_ZONE_NAME_UNIQUE_ERROR_CODE else NON_FIELD_ERRORS
                errors.setdefault(field, []).append(non_field_error)
            raise ValidationError(errors) from error

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
                # Compared with the serial as loaded, not the stored one, so an increment made by
                # another transaction since the load is not mistaken for a manual change.
                if initial is not None and self.soa_serial != initial:
                    raise ValidationError({"soa_serial": SOA_SERIAL_MANAGED_MESSAGE})
            elif self.soa_serial != self._meta.get_field("soa_serial").default:
                # A new zone starts at the default. Onboard a zone at a specific serial with
                # auto-increment off.
                raise ValidationError({"soa_serial": SOA_SERIAL_MANAGED_MESSAGE})

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


def rebuild_zone_hierarchy(dns_view=None):
    """Recompute the parent of every zone, or of every zone in `dns_view`, and return how many changed.

    `DNSZone.save()` and zone deletion call this for the affected view. Run it directly to repair parents after
    writes that bypass them, such as `QuerySet.update()` or `bulk_create()`.
    """
    zones = DNSZone.objects.only("pk", "name", "dns_view_id", "parent_id")
    if dns_view is not None:
        zones = zones.filter(dns_view=dns_view)
    zone_list = list(zones)
    parents = compute_zone_parents((zone.pk, zone.name, zone.dns_view_id) for zone in zone_list)

    changed = [zone for zone in zone_list if zone.parent_id != parents[zone.pk]]
    for zone in changed:
        zone.parent_id = parents[zone.pk]
    DNSZone.objects.bulk_update(changed, ["parent"], batch_size=1000)
    return len(changed)


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
    mail_server = models.CharField(max_length=200, help_text="FQDN of the Mail server.")

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

    text = models.TextField(help_text="Text for the TXT Record.")
    # SHA-256 hash of `text` for the unique constraint: MySQL cannot index a TEXT column without a prefix (error 1170).
    # The leading underscore keeps it out of the REST API, as with `_ttl`.
    _text_hash = models.CharField(
        max_length=64, editable=False, help_text="SHA-256 digest of `text`, used to enforce uniqueness."
    )

    # Declared explicitly, or Nautobot would derive the natural key from `unique_together` and use `_text_hash`.
    natural_key_field_names = ["name", "text", "zone"]

    class Meta:
        """Meta attributes for TXTRecord."""

        unique_together = [["name", "zone", "_text_hash"]]
        verbose_name = "TXT Record"
        verbose_name_plural = "TXT Records"

    @staticmethod
    def hash_text(value):
        """Return the SHA-256 digest used to enforce `text` uniqueness."""
        return hashlib.sha256((value or "").encode("utf-8")).hexdigest()

    @property
    def character_strings(self):
        """Return `text` as the list of <character-string> values it represents on the wire, each as `bytes`."""
        return split_txt_character_strings(self.text)

    def clean(self):
        """Refresh the uniqueness digest and validate `text` per RFC 1035 §3.3.14 if wire-format validation is enabled.

        The digest is set here, not only in `save()`, because `full_clean()` runs `clean()` before `validate_unique()`.
        """
        super().clean()

        self._text_hash = self.hash_text(self.text)

        validation_level = getattr(constance_config, "nautobot_dns_models__DNS_VALIDATION_LEVEL")
        if validation_level != "wire-format":
            return

        # Raises a ValidationError on `text` for malformed presentation format, e.g. an unterminated quote.
        character_string_list = split_txt_character_strings(self.text)
        for position, character_string in enumerate(character_string_list, start=1):
            octets = len(character_string)
            if octets > TXT_CHARACTER_STRING_MAX_OCTETS:
                raise ValidationError(
                    {
                        "text": (
                            f"Character-string {position} is {octets} bytes (octets) but the maximum is {TXT_CHARACTER_STRING_MAX_OCTETS}. "
                            'Split the value into multiple double-quoted character-strings, e.g. "part one" "part two".'
                        )
                    }
                )

        wire_length = txt_wire_rdata_length(character_string_list)
        if wire_length > TXT_RDATA_MAX_OCTETS:
            raise ValidationError(
                {
                    "text": (
                        f"Total TXT record data is {wire_length} bytes (octets) in wire format; the maximum is {TXT_RDATA_MAX_OCTETS}."
                    )
                }
            )

    def unique_error_message(self, model_class, unique_check):
        """Report a uniqueness violation in terms of `text` rather than the internal `_text_hash`."""
        if set(unique_check) == {"name", "zone", "_text_hash"}:
            return ValidationError(
                message="TXT Record with this Name, Zone and Text already exists.",
                code="unique_together",
            )
        return super().unique_error_message(model_class, unique_check)

    def save(self, *args, **kwargs):
        """Refresh the uniqueness digest before saving, for callers that bypass `clean()`."""
        self._text_hash = self.hash_text(self.text)
        # A caller updating only `text` would otherwise leave the digest behind in the database.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "text" in update_fields:
            kwargs["update_fields"] = list(update_fields) + ["_text_hash"]
        super().save(*args, **kwargs)


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
