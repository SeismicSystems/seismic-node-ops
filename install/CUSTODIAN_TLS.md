# Custodian HTTPS deployment

Both node installers use the enclave `centralized-custodian` branch. Custodian
binds to **`127.0.0.1:7876`** behind a same-host TLS terminator. Never expose
port 7876 externally; the installer does not configure firewall rules.

## Installer choices

| Mode                                  | HTTPS routes                                                  |
| ------------------------------------- | ------------------------------------------------------------- |
| Own TLS terminator                    | Operator-configured; the installer leaves the proxy untouched |
| OpenResty: Custodian only (default)   | Custodian only                                                |
| OpenResty: Custodian + node endpoints | Custodian, RPC, Summit, and the other node routes             |

Managed modes configure automatic certificates and write the entire OpenResty
main configuration. Custodian-only mode skips JWT setup. Custodian uses protocol
signatures, not proxy JWTs, to authorize operations. Its HTTPS frontend is
Internet-reachable without a source-IP allowlist.

## Endpoint

Give Seismic operations your domain. We will reach your Custodian at:

```text
https://YOUR_DOMAIN/custodian
```

When installing an observer, enter the **parent validator's** Custodian endpoint
when prompted—not the observer's own endpoint.

Clients append `/v1/council`; the proxy forwards `POST /custodian/v1/council` to
`http://127.0.0.1:7876/v1/council`. Do not include `/v1/council` in the base
URL.

Clients require trusted HTTPS certificates and do not follow redirects. Loopback
HTTP is allowed for a local secure tunnel; remote plaintext HTTP is rejected.
Base URLs must not contain credentials, queries, or fragments.

## Proxy requirements

Request and response bodies contain root keys. An operator-managed terminator
must run on the same host and:

- Forward CBOR requests to `/v1/council`, stripping the public prefix and
  upgrade headers. Do not require proxy JWTs or HTTP credentials.
- Limit bodies to 64 KiB; bound headers, connections, request rates, and upload
  duration. Receive complete requests in memory before forwarding.
- Never log, cache, or spill secret bodies to disk.
- Route requests to one Custodian instance: observer challenges are
  process-local.

Managed OpenResty applies these protections with dedicated limits:

| Protection       | Limit                                                       |
| ---------------- | ----------------------------------------------------------- |
| Body size        | 64 KiB; `Content-Length` required, chunked uploads rejected |
| Upload deadline  | 5 seconds total                                             |
| Active requests  | 4 per IP, 16 globally                                       |
| Request rate     | 5/s per IP, burst 10; 20/s globally, burst 20               |
| Backend timeouts | 1-second connect; 5-second send/read inactivity             |

The CLI and observer send fixed-length bodies. Custodian access logging,
caching, and disk buffering are disabled. Invalid paths and plaintext HTTP
requests are rejected. These limits do not replace upstream DDoS protection.

See `templates/openresty/custodian-*.conf` and
`templates/openresty/lua/custodian.lua` for the complete OpenResty-specific
policy.

## Deployment checklist

The installer prepares configuration but does **not** start services.

1. Install Custodian, choose a TLS option, and give Seismic operations your
   domain.
2. Point DNS at the node. For managed OpenResty, allow inbound TCP **80 and
   443**; keep **7876** private.
3. Start Custodian using the node installer's service instructions. Verify its
   loopback binding with `sudo ss -ltnp '( sport = :7876 )'`.
4. Start your TLS terminator. For managed OpenResty:

   ```bash
   sudo openresty -t
   sudo systemctl enable --now openresty
   ```

5. Verify TLS with the following command. Expect **405** because the endpoint
   requires POST. Seismic operations handles council-side connectivity checks.

   ```bash
   curl -i https://YOUR_DOMAIN/custodian/v1/council
   ```

6. Start dependent observers only after the **parent's HTTPS endpoint is
   ready**.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_custodian*.py' -v
```

Set `CUSTODIAN_OPENRESTY_BIN` to enable isolated TLS proxy tests. Optionally set
`CUSTODIAN_SERVICE_BIN` and `COUNCIL_SIGNER_BIN` for a real protocol smoke test.
Tests use temporary loopback services; public certificate issuance must be
verified during deployment.
