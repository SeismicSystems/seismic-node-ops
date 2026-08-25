# Seismic Validator Installer and First-Start Guide

This directory contains the interactive installer for a Seismic validator node:

```text
install/install-validator.sh
```

The installer prepares the validator software, persistent directories, keys,
Supervisor programs, and optional OpenResty endpoint. It deliberately does not
start the validator services. Follow the first-start procedure in this document
after installation.

## Safety model

The installer is designed to avoid replacing persistent validator state:

- Validator services run as an existing non-root service user.
- Persistent directories must be absolute, non-root, and non-overlapping.
- Existing Reth P2P and Summit validator keys are preserved.
- An incomplete Summit key pair causes the installer to stop rather than
  regenerate either key.
- Service binaries are installed as root-owned, non-writable executables.
- Supervisor programs use `autostart=false` and `autorestart=false`.
- The installer does not start, enable, reread, update, reload, or restart
  validator services.

Review the complete interactive configuration summary before accepting it.

## Requirements

- Ubuntu or Debian with `apt-get`.
- Root access through `sudo`.
- An existing non-root Linux user that will run the validator services.
- The Summit internal-testnet genesis TOML supplied in this repository at
  `internal_testnet_genesis.toml`; it must remain readable by the service user.
- Sufficient persistent storage for Reth, Summit, validator keys, and any
  enabled optional components.
- Network access to package and source repositories when installing packages or
  building from source.
- If configuring OpenResty, DNS for the selected domain must point to the node,
  and inbound TCP ports 80 and 443 must be reachable for HTTPS and certificate
  provisioning.

The installation log is written to:

```text
/var/log/seismic-validator-install.log
```

### Supplied internal-testnet genesis

The repository includes the Summit genesis file for the internal testnet:

```text
internal_testnet_genesis.toml
```

When the installer asks for the Summit genesis TOML path, enter its absolute
path. From the repository root, obtain it with:

```bash
realpath internal_testnet_genesis.toml
```

Keep this file in place after installation. The generated Supervisor programs
reference the selected path directly, and the configured service user must be
able to read it.

## Run the installer

Run the installer from the repository root:

```bash
sudo ./install/install-validator.sh
```

The installer asks you to configure:

- The non-root service user.
- Persistent Reth, Summit, and validator-key directories.
- An optional public HTTPS endpoint through OpenResty.
- The Summit genesis file and an optional bootnode RPC.
- Summit and seismic-reth binary installation methods.
- summit-checkpointer.
- Centralized Custodian.

When Centralized Custodian is enabled, the installer always uses its publicly
known shared default root key and does not prompt for a custom key. This makes
epoch-0 purpose keys public.

The Custodian council listener defaults to `0.0.0.0:7876`. TCP port `7876`
must be reachable from outside the node for key rotation.
Configure the cloud firewall, security group, and host firewall as
needed, and restrict allowed source addresses to the intended council
participants rather than exposing the port more broadly than necessary.

For Summit, seismic-reth, and summit-checkpointer, supported installation modes
are:

1. Install a supplied prebuilt executable.
2. Build from source during installation.
3. Defer installation and provide the executable later.

The current source-build defaults are:

```text
Summit:       m/metrics
seismic-reth: feat/purpose-key-rotation-reth
```

Deferred binaries must be installed at the configured target paths before the
corresponding services are started.

## Persistent layout

The default persistent paths are:

| State | Default path |
| --- | --- |
| Reth data | `/persistence/reth` |
| Summit data | `/persistence/summit` |
| Validator keys | `/persistence/keys` |
| Checkpointer output, when enabled | `/persistence/checkpoints` |
| Custodian data, when enabled | `/persistence/custodian` |

Important files derived from those paths include:

```text
<persistence keys>/reth/p2p-key
<persistence keys>/summit/node_key.pem
<persistence keys>/summit/consensus_key.pem
```

Back up the validator keys securely. Do not copy them into tickets, chat
messages, source control, or the deposit-signature file described below.

### Reth MDBX database

seismic-reth stores its MDBX execution database under:

```text
<RETH_DATA_DIR>/db
```

Treat this as persistent execution-layer state. Do not delete it during an
installer rerun, and do not manually copy or modify the live database while
Reth is running.

When summit-checkpointer is enabled, it uses `mdbx_copy` to produce a consistent
Reth database copy. `mdbx_copy` must be built from the same vendored libmdbx
revision as the seismic-reth binary. If Reth is built from source, the installer
builds `mdbx_copy` from that checkout. For a prebuilt or deferred Reth binary,
provide the matching seismic-reth source checkout when prompted. Do not start
the checkpointer until a compatible executable is installed at:

```text
/usr/local/bin/mdbx_copy
```

## Generated services and configuration

The installer writes the Supervisor configuration to:

```text
/etc/supervisor/conf.d/seismic-validator.conf
```

It always defines:

- `summit-deposit-rpc`
- `reth`
- `summit`

It also defines `checkpointer` and `custodian` when those components are
enabled. Supervisor logs are written under:

```text
/var/log/seismic-validator/
```

When OpenResty is enabled, the installer writes:

```text
/usr/local/openresty/nginx/conf/nginx.conf
/usr/local/openresty/nginx/lua/rate_limit.lua
/usr/local/openresty/nginx/lua/jwt_auth.lua
/etc/seismic/openresty-jwt-secret
/etc/logrotate.d/openresty
```

The JWT secret is root-owned and is not printed by the installer.

## First validator startup

Do not start the full validator until the deposit-signature file has been
created and Seismic operations has confirmed that the staking transaction was
successful.

### 1. Load the Supervisor configuration

```bash
sudo systemctl enable --now supervisor
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl status
```

All validator programs should still be stopped because they use:

```ini
autostart=false
autorestart=false
```

### 2. Start only the deposit-signature RPC

```bash
sudo supervisorctl start summit-deposit-rpc
sudo supervisorctl status summit-deposit-rpc
```

This temporary process uses the configured Summit genesis file and validator
keys. It listens only on:

```text
http://127.0.0.1:3031
```

It does not start Reth, Summit consensus, P2P, storage, or synchronization, and
it is not exposed through OpenResty.

If it does not start, inspect:

```bash
sudo tail -n 100 /var/log/seismic-validator/summit-deposit-rpc.err
sudo tail -n 100 /var/log/seismic-validator/summit-deposit-rpc.log
```

### 3. Confirm the required withdrawal address

The deposit-signature request below is hardcoded to use:

```text
0xd412c5ecd343e264381ff15afc0ad78a67b79f35
```

Do not replace this value with another address. Verify it carefully before
requesting the signature because the deposit binds validator withdrawals and
exits to the encoded address.

### 4. Save the signed deposit response as JSON

Run the following on the validator server while `summit-deposit-rpc` is
running:

```bash
curl --fail-with-body --silent --show-error \
  http://127.0.0.1:3031 \
  -H 'Content-Type: application/json' \
  --data '{
    "jsonrpc": "2.0",
    "method": "getDepositSignature",
    "params": [
      32000000000,
      "0xd412c5ecd343e264381ff15afc0ad78a67b79f35"
    ],
    "id": 1
  }' \
  --output deposit-signature.json

chmod 0600 deposit-signature.json
jq . deposit-signature.json
```

The amount `32000000000` is denominated in gwei and represents a 32 ETH
validator stake.

The response contains the public keys, withdrawal credentials, signatures, and
deposit root needed to stake. It must not contain the node private key,
consensus private key, Reth P2P key, OpenResty JWT secret, or any
funding-account private key.

### 5. Stop the temporary RPC

```bash
sudo supervisorctl stop summit-deposit-rpc
sudo supervisorctl status summit-deposit-rpc
```

Do not leave the signing endpoint running after obtaining the file.

### 6. Send the signature file to Seismic operations

Send the following through the secure transfer channel agreed with Seismic
operations:

- `deposit-signature.json`
- The validator/node name.
- The target network.
- Confirmation that you used withdrawal address
  `0xd412c5ecd343e264381ff15afc0ad78a67b79f35`.

Do **not** send validator private-key files, a wallet private key, seed phrases,
or JWT secrets. Do not publish the signature file in a public issue or source
repository.

Seismic operations will submit the staking transaction on your behalf. Wait
for the transaction hash and confirmation that its receipt has status
`0x1` before starting the full validator.


### 7. Start the validator after staking confirmation

Start only the components enabled during installation, in this order:

```bash
# Run this first only when Custodian was configured.
sudo supervisorctl start custodian

sudo supervisorctl start reth
sudo supervisorctl start summit

# Optional: Run this last only when summit-checkpointer was configured.
sudo supervisorctl start checkpointer

sudo supervisorctl status
```

Because `autostart=false` and `autorestart=false`, these programs must be
started manually again after a server or Supervisor restart.

## OpenResty public endpoint

When enabled, OpenResty terminates HTTPS, obtains certificates through
`lua-resty-auto-ssl`, applies per-client rate limiting, and proxies local Reth
and Summit endpoints. Reth and Summit bind their proxied RPC listeners to
loopback when the public endpoint is enabled.

The configured routes are:

| Public path | Local upstream | Notes |
| --- | --- | --- |
| `/` | `127.0.0.1:3000` | Grafana |
| `/staking` | `/var/www/html/staking` | Static staking UI |
| `/rpc` | `127.0.0.1:8545` | Reth HTTP JSON-RPC |
| `/ws` | `127.0.0.1:8546` | Reth WebSocket JSON-RPC |
| `/summit` | `127.0.0.1:3030` | Summit RPC |
| `/ops` | `127.0.0.1:8552` | Signature-authenticated privileged Reth RPC |
| `/prom-summit` | `127.0.0.1:9090` | Rate-limited and JWT-protected |
| `/prom-reth` | `127.0.0.1:9001` | Rate-limited and JWT-protected |

The localhost-only `summit-deposit-rpc` service on port 3031 is deliberately
not proxied.

### Start or reload OpenResty

```bash
sudo openresty -t
sudo systemctl enable openresty

if sudo systemctl is-active --quiet openresty; then
  sudo systemctl reload openresty
else
  sudo systemctl start openresty
fi

sudo systemctl status openresty --no-pager
```

## Safe installer reruns

The installer can be rerun to update binaries or rendered configuration, but it
does not persist previous answers. Re-enter the same service user, persistent
directories, genesis path, and optional component settings unless you are
intentionally changing them.

On a normal rerun, the installer preserves existing validator keys and state,
then replaces its generated OpenResty and Supervisor configuration. It does not
start or reload those services.

Before applying a later `supervisorctl update`, inspect running services:

```bash
sudo supervisorctl status
```

Supervisor may restart a running program whose definition changed when the
updated configuration is applied. Plan that operation separately for a live
validator.
