# DNS Zone Model

The DNS zone model is used to represent a distinct DNS zone. It contains the zone name, TTL, and SOA record details.

- `name` (string): Unique FQDN of the Zone, w/ TLD. e.g `example.com`.
- `ttl` (integer): Time to live for the DNS zone.
- `filename` (string): Filename of the DNS zone file.
- `description`: (string): Description of the DNS zone.
- `soa_mname`: (string): FQDN of the authoritative name server for the DNS zone.
- `soa_rname`: (string): Email address of the administrator for the DNS zone.
- `soa_refresh`: (integer): Time in seconds for secondary name servers to query the master for the SOA record.
- `soa_retry`: (integer): Time in seconds for secondary name servers to retry to request the serial number from the master.
- `soa_expire`: (integer): Time in seconds for secondary name servers to stop answering requests if the master does not respond. This value must be bigger than the sum of refresh and retry.
- `soa_serial`: (integer): Serial number of the zone. This value must be incremented each time the zone is changed, and secondary DNS servers must be able to retrieve this value to check if the zone has been updated.
- `soa_minimum`: (integer): Minimum TTL for records in this zone.

+++ 1.2.0 "DNS label length rules"

When DNS validation is enabled (via the `DNS_VALIDATION_LEVEL` configuration), `DNSZone` enforces the following DNS label length rules, as specified by [RFC 1035 §3.1](https://datatracker.ietf.org/doc/html/rfc1035#section-3.1):

- Each label (the parts of the name separated by dots) must be no more than 63 bytes in wire format
- Empty labels (e.g., consecutive dots or leading/trailing dots) are not allowed

See the [installation guide](../admin/install.md#app-configuration) for configuration options.

## SOA Serial Auto-Increment

When enabled via the `SOA_SERIAL_AUTO_INCREMENT` [configuration option](../admin/install.md#app-configuration), the `soa_serial` field advances automatically whenever the zone's DNS-serving data changes. It is disabled by default.

- **Record changes**: creating or deleting any record type in the zone, moving a record to another zone, or changing a record's published DNS data (its `name`, TTL, or value). Changing only a record's `description`, `comment`, tags or custom fields does not increment.
- **Zone changes**: changing the zone's `name`, `dns_view`, `ttl`, `filename`, `soa_mname`, `soa_rname`, `soa_refresh`, `soa_retry`, `soa_expire` or `soa_minimum`. Changing only the zone's `description`, tags or custom fields does not increment.

Increments are coalesced per transaction: any number of qualifying changes to one zone within one database transaction advance its serial by exactly one. A record move increments both the old and the new zone.

While auto-increment is enabled, the serial is not manually editable. The GUI disables the field, the REST API ignores a submitted `soa_serial`, and model validation rejects a manual change. A new zone starts at the default serial of 0 and moves to 1 on its first increment. To set a serial explicitly, disable auto-increment first.

!!! note
    `QuerySet.bulk_create()`, `QuerySet.bulk_update()`, `QuerySet.update()` and raw SQL bypass auto-increment because they do not call the model's `save()` method. Deletes are covered, including `QuerySet.delete()` and the bulk delete action in the UI.

!!! note
    The rule that a new zone starts at the default serial is enforced by model validation (`full_clean()` / `validated_save()`), the GUI and the REST API. A direct `DNSZone.objects.create(soa_serial=...)` stores the serial it is given.

!!! warning "Serial range"
    The `soa_serial` column holds values up to 2,147,483,647, half of the 32-bit serial space DNS defines. A serial at that value wraps to 1 on its next increment, which a secondary comparing serials per [RFC 1982](https://datatracker.ietf.org/doc/html/rfc1982) reads as older, not newer. Date-based serials (`YYYYMMDDNN`) stay below this limit.

!!! note
    An increment locks the zone's row until the surrounding transaction ends, so a long transaction that changes a zone's records makes other writers to the same zone wait.
