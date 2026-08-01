# DNS Zone Model

The DNS zone model is used to represent a distinct DNS zone. It contains the zone name, TTL, and SOA record details.

Domain registration attributes are modeled separately in `DNSRegistration`.

- `name` (string): Unique FQDN of the Zone, w/ TLD. e.g `example.com`.
- `ttl` (integer): Time to live for the DNS zone.
- `filename` (string): Filename of the DNS zone file.
- `description`: (string): Description of the DNS zone.
- `soa_mname`: (string): FQDN of the authoritative name server for the DNS zone.
- `soa_rname`: (string): Email address of the administrator for the DNS zone.
- `soa_refresh`: (integer): Time in seconds for secondary name servers to query the master for the SOA record.
- `soa_retry`: (integer): Time in seconds for secondary name servers to retry to request the serial number from the master.
- `soa_expire`: (integer): Time in seconds for secondary name servers to stop answering requests if the master does not respond. This value must be bigger than the sum of refresh and retry.
- `soa_serial`: (integer): Serial number of the zone (unsigned 32-bit, 0–4,294,967,295, per [RFC 1035 §3.3.13](https://datatracker.ietf.org/doc/html/rfc1035#section-3.3.13) and [RFC 1982 §7](https://datatracker.ietf.org/doc/html/rfc1982#section-7)). This value must be incremented each time the zone is changed, and secondary DNS servers must be able to retrieve this value to check if the zone has been updated. See [SOA Serial Auto-Increment](#soa-serial-auto-increment) below.
- `soa_minimum`: (integer): Minimum TTL for records in this zone.
- `tenant` (Tenant, optional): Reference to the Tenant model for multi-tenancy support.
- `auto_create_ptr` (boolean, default `False`): When enabled, creating an A or AAAA record in this zone automatically creates a matching PTR record in the most-specific reverse zone within the same DNS view. If no matching reverse zone exists, the A/AAAA creation fails with a validation error.

+++ 1.2.0 "DNS label length rules"

    When DNS validation is enabled (via the `DNS_VALIDATION_LEVEL` configuration), `DNSZone` enforces the following DNS label length rules, as specified by [RFC 1035 §3.1](https://datatracker.ietf.org/doc/html/rfc1035#section-3.1):

    - Each label (the parts of the name separated by dots) must be no more than 63 bytes in wire format
    - Empty labels (e.g., consecutive dots or leading/trailing dots) are not allowed

    See the [installation guide](../admin/install.md#app-configuration) for configuration options.

+++ 2.3.0 "SOA serial auto-increment"

    ## SOA Serial Auto-Increment

    When enabled via the `SOA_SERIAL_AUTO_INCREMENT` [configuration option](../admin/install.md#app-configuration), the `soa_serial` field is automatically incremented by 1 whenever zone data changes. This includes:

    - **Record changes** — Creating, updating, or deleting any record type (A, AAAA, PTR, CNAME, NS, MX, SRV, TXT) in the zone.
    - **Zone self-changes** — Modifying zone fields such as `name`, `ttl`, `filename`, `soa_mname`, `soa_rname`, `soa_refresh`, `soa_retry`, `soa_expire`, or `soa_minimum`.

    Changes to `description` or `tenant` do **not** trigger a serial increment, as these fields are not part of the DNS zone data served to secondary name servers.

    While auto-increment is enabled, manual changes to `soa_serial` via the API or GUI are rejected with a validation error. To set the serial explicitly, disable auto-increment first.

    The `soa_serial` field accepts values in the range 0–4,294,967,295 (unsigned 32-bit, per [RFC 1035 §3.3.13](https://datatracker.ietf.org/doc/html/rfc1035#section-3.3.13) and [RFC 1982 §7](https://datatracker.ietf.org/doc/html/rfc1982#section-7)). When auto-increment reaches the maximum value, the serial wraps to **1** (not 0), following [RFC 2136 §7.11](https://datatracker.ietf.org/doc/html/rfc2136#section-7.11) for maximum compatibility with DNS implementations.

    !!! note
        `QuerySet.bulk_create()`, `QuerySet.bulk_update()`, `QuerySet.update()`, `QuerySet.delete()`, and raw SQL all bypass auto-increment because they do not call the individual model `save()` / `delete()` methods. Use individual `.save()` / `.delete()` calls or the REST API for operations that require serial tracking.
