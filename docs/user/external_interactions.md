# External Interactions

There are no external interactions at this time, but some are planned in the future. 

- Create a BIND9 Zone file
- Infoblox SoTT App integration

## External System Integrations

### From the App to Other Systems

The DNS models can be used to populate DNS systems with records and configurations.

### From Other Systems to the App

The DNS related configuration from other systems such as Infoblox, Bluecat, or BIND can be modeled within Nautobot.

## Nautobot REST API endpoints

Create a DNS Zone

```bash
curl -X 'POST' http://$NAUTOBOT_HOST/api/plugins/dns/dns-zones/ \
-H 'Content-Type: application/json' \
-H "Authorization: Token $NAUTOBOT_API_TOKEN" \
-d '{
  "name": "nautobot.com",
  "dns_view": "'"$DNS_VIEW_ID"'",
  "ttl": 3600,
  "filename": "nautobot.com",
  "soa_mname": "ns1.cloudns.net",
  "soa_rname": "admin@nautobot.com",
  "soa_refresh": 86400,
  "soa_retry": 7200,
  "soa_expire": 3600000,
  "soa_serial": 1,
  "soa_minimum": 3600
}'
```

`$DNS_VIEW_ID` is the UUID of an existing DNS View (see the DNS Views API endpoint). With SOA serial auto-increment enabled, a submitted `soa_serial` is ignored and the new zone starts at serial `1`.

Add an A record to a DNS Zone.

```bash
curl -X 'POST' \
  http://$NAUTOBOT_HOST/api/plugins/dns/a-records/ \
  -H 'Content-Type: application/json' \
  -H "Authorization: Token $NAUTOBOT_API_TOKEN" \
  -d '{
  "name": "dns-zone.example",
  "description": "Main Web Server",
  "zone": {
    "name": "nautobot.com2"
  },
  "ip_address": {
    "id": "185fb1e5-ac1f-40d2-9813-ca4a44df36c4"
  }
}'
```