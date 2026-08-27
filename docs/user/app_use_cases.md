# Using the App

This document describes common use-cases and scenarios for this App.

## General Usage

Generally, this App is to used to keep track of DNS Zones and their related data and records. The idea is to have a set of models that can either be used to directly model DNS configurations or as a place to aggregate data about DNS related configuration from other tools. While A, AAAA, and PTR records are already partially supported within Nautobot core models, this App is meant to expand the type of DNS records and formalize DNS models within Nautobot.

## Use-cases and common workflows

Use the API or GUI to add DNS Zones and Record objects to Nautobot.

### SOA Serial Number Auditing

When `SOA_SERIAL_AUTO_INCREMENT` is [enabled](../admin/install.md#app-configuration), the app automatically tracks zone changes by advancing the SOA serial number whenever DNS-serving data changes — a record is created or deleted, a record's published DNS data (name, TTL, `enabled`, or value) changes, or a DNS-serving zone field (name, `enabled`, TTL, SOA parameters, or filename) changes. Changes to Nautobot-only metadata (a record's or zone's description, comment, tenant, or custom fields) do not advance the serial. Increments are coalesced per transaction: any number of qualifying changes to a single zone in one transaction advance its serial by exactly one. See the [DNS Zone model documentation](../models/dnszone.md#soa-serial-auto-increment) for full details. This allows operators to:

- **Detect zone drift** — Compare the serial in Nautobot with the serial served by your authoritative DNS servers to identify zones that are out of sync.
- **Audit change frequency** — Use the serial number as a proxy for how actively a zone is being modified.
- **Trigger downstream automation** — Use Nautobot webhooks on `DNSZone` changes to notify external systems (e.g., DNS provisioning pipelines) when a zone's serial increments, indicating new data is ready to be pushed.

## Screenshots

![Adding a DNS Zone](../images/getting_started-add-zone-3-light.png#only-light){ .on-glb }
![Adding a DNS Zone](../images/getting_started-add-zone-3-dark.png#only-dark){ .on-glb }
[//]: # "`https://next.demo.nautobot.com/plugins/dns/dns-zones/add/`"

/// caption
Adding a DNS Zone
///

![DNS Zone View](../images/getting_started-add-record-3-light.png#only-light){ .on-glb }
![DNS Zone View](../images/getting_started-add-record-3-dark.png#only-dark){ .on-glb }
/// caption
The DNS Zone View
///
