# DNS Zone Model

The DNS zone model is used to represent a distinct DNS zone. It contains the zone name, TTL, and SOA record details.

Domain registration attributes are modeled separately in `DNSRegistration`.

- `name` (string): Unique FQDN of the Zone, w/ TLD. e.g `example.com`.
- `enabled` (boolean, default `True`): Indicates whether the zone is eligible for publication by external integrations. This app does not publish zones or enforce this setting.
- `ttl` (integer): Time to live for the DNS zone.
- `filename` (string): Filename of the DNS zone file.
- `description`: (string): Description of the DNS zone.
- `soa_mname`: (string): FQDN of the authoritative name server for the DNS zone.
- `soa_rname`: (string): Mailbox or single-label placeholder for the person responsible for the DNS zone.
- `soa_refresh`: (integer): Time in seconds for secondary name servers to query the primary for the SOA record.
- `soa_retry`: (integer): Time in seconds for secondary name servers to retry to request the serial number from the primary.
- `soa_expire`: (integer): Time in seconds for secondary name servers to stop answering requests if the primary does not respond. This value must be bigger than the sum of refresh and retry.
- `soa_serial`: (integer, default `1`): Serial number of the zone (unsigned 32-bit, per [RFC 1035 §3.3.13](https://datatracker.ietf.org/doc/html/rfc1035#section-3.3.13) and [RFC 1982 §7](https://datatracker.ietf.org/doc/html/rfc1982#section-7)). New zones and intentional changes must use values **1–4,294,967,295**: [RFC 2136 §7.11](https://datatracker.ietf.org/doc/html/rfc2136#section-7.11) states a zone's SOA SERIAL should never be set to 0, due to interoperability problems with some older but widely installed DNS implementations. Unchanged legacy zones already at serial 0 remain editable. This value must be incremented each time the zone is changed, and secondary DNS servers must be able to retrieve this value to check if the zone has been updated. See [SOA Serial Auto-Increment](#soa-serial-auto-increment) below.
- `soa_minimum`: (integer): Minimum TTL for records in this zone.
- `tenant` (Tenant, optional): Reference to the Tenant model for multi-tenancy support.
- `auto_create_ptr` (boolean, default `False`): When enabled, creating an A or AAAA record in this zone automatically creates a matching PTR record in the most-specific reverse zone within the same DNS view. If no matching reverse zone exists, the A/AAAA creation fails with a validation error.

## SOA RNAME Formats

An SOA RNAME may be entered as an email address, a basic DNS-style mailbox, or a single-label placeholder. Email-style values must be valid email addresses. Basic DNS-style mailboxes are normalized to email form prior to being saved, and single-label placeholders are stored without a trailing dot.

DNS-style mailbox decoding follows [RFC 1035 §3.3.13](https://www.rfc-editor.org/rfc/rfc1035.html#section-3.3.13)
and [§8](https://www.rfc-editor.org/rfc/rfc1035.html#section-8). This app stores decoded mailbox values in
email form as follows:

| Input | Stored value | Result |
| --- | --- | --- |
| `admin@example.com` | `admin@example.com` | Accepted unchanged |
| `admin.example.com.` | `admin@example.com` | Normalized from DNS style |
| `john\.smith.example.com.` | `john.smith@example.com` | The `\.` escape becomes a dot in the mailbox name per [RFC 1035 §5.1](https://www.rfc-editor.org/rfc/rfc1035.html#section-5.1) |
| `invalid.` | `invalid` | Single-label placeholder normalized |
| `john@example` | — | Rejected because the email domain is not fully qualified |
| `admin\046example.com.` | — | Rejected because the DNS escape is unsupported |
| `admin..example.com` | — | Rejected because the DNS-style mailbox is malformed |

+++ 1.2.0 "DNS label length rules"

    When DNS validation is enabled (via the `DNS_VALIDATION_LEVEL` configuration), `DNSZone` enforces the following DNS label length rules, as specified by [RFC 1035 §3.1](https://datatracker.ietf.org/doc/html/rfc1035#section-3.1):

    - Each label (the parts of the name separated by dots) must be no more than 63 bytes in wire format
    - Empty labels (e.g., consecutive dots or leading/trailing dots) are not allowed

    See the [installation guide](../admin/install.md#app-configuration) for configuration options.

## SOA Serial Auto-Increment

When enabled via the `SOA_SERIAL_AUTO_INCREMENT` [configuration option](../admin/install.md#app-configuration), the `soa_serial` field is automatically incremented by 1 whenever zone data changes. This includes:

- **Record changes** — Creating, updating, or deleting any record type (A, AAAA, PTR, CNAME, NS, MX, SRV, TXT) in the zone. Record `description` and `comment` changes are included.
- **Zone self-changes** — Modifying zone fields such as `name`, `ttl`, `filename`, `soa_mname`, `soa_rname`, `soa_refresh`, `soa_retry`, `soa_expire`, or `soa_minimum`.

Changes to the zone's `description`, `tenant`, `dns_view`, or `auto_create_ptr` do **not** trigger a serial increment, as these fields are not part of the zone data written into DNS records served to secondary name servers.

A record saved without `update_fields` always increments the serial. A record saved with non-empty `update_fields` also increments, including when the only updated fields are `description` or `comment`; `update_fields=[]` remains a Django no-op and does not write or increment.

While auto-increment is enabled, manual changes to `soa_serial` via the API or GUI are rejected with a validation error. To set the serial explicitly, disable auto-increment first.

The `soa_serial` field accepts new or changed values in the range 1–4,294,967,295 (unsigned 32-bit, per [RFC 1035 §3.3.13](https://datatracker.ietf.org/doc/html/rfc1035#section-3.3.13) and [RFC 1982 §7](https://datatracker.ietf.org/doc/html/rfc1982#section-7)). [RFC 2136 §7.11](https://datatracker.ietf.org/doc/html/rfc2136#section-7.11) states the serial should never be set to 0, so new zones default to **1** and intentional changes to 0 are rejected. Unchanged legacy zones already at serial 0 remain editable and move to 1 on their first auto-increment. For the same reason, when auto-increment reaches the maximum value the serial wraps to **1** rather than 0.

!!! note
    `QuerySet.bulk_create()`, `QuerySet.bulk_update()`, `QuerySet.update()`, and raw SQL bypass auto-increment because they do not call the individual model `save()` method. Use individual `.save()` calls or the REST API for write operations that require serial tracking.

    Deletion is **not** subject to this limitation. Record deletions are tracked through `post_delete`, so `QuerySet.delete()` — including the "Delete Selected" bulk action in the UI and bulk deletes through the REST API — increments the serial correctly. Records removed by a cascade, such as an A record deleted along with its IP address, are covered as well. To preserve correct behavior if a record is moved concurrently with deletion, each deleted record is fetched and locked before it is removed, so very large bulk deletes carry an extra per-record query and row-lock cost.
