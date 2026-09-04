# Seismic Validator Installer and First-Start Guide

This directory contains the interactive installer for a Seismic validator node:

```text
install/install-validator.sh
```

The installer prepares the validator software, persistent directories, keys,
Supervisor programs, and optional OpenResty endpoint. It deliberately does not
start the validator services. Follow the first-start procedure in this document
after installation.

For an observer node, use `install/install-observer.sh` and follow the separate
**[Observer Installer and First-Start Guide](OBSERVER.md)**.

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

- Ubuntu 24.04 LTS (Noble) with `apt-get`.
- Root access through `sudo`.
- An existing non-root Linux user that will run the validator services.
- The Summit internal-testnet genesis TOML supplied in this repository at
  `internal_testnet_genesis.toml`; it must remain readable by the service user.
- Sufficient persistent storage for Reth, Summit, validator keys, and any
  enabled optional components.
- Network access to package and source repositories when installing packages or
  building from source.
- If configuring OpenResty, DNS for the selected domain must point to the node.

### Network and firewall requirements

The installer does not configure cloud firewall, security-group, or host
firewall rules. Configure the required inbound access before starting the
validator.

| Port    | Protocol    | Purpose                          | Required exposure                                                                                 |
| ------- | ----------- | -------------------------------- | ------------------------------------------------------------------------------------------------- |
| `30303` | TCP and UDP | seismic-reth P2P and discovery   | Public                                                                                            |
| `18551` | TCP and UDP | Summit consensus P2P             | Public                                                                                            |
| `80`    | TCP         | HTTP redirect and ACME challenge | Public when OpenResty is enabled                                                                  |
| `443`   | TCP         | OpenResty HTTPS endpoint         | Public when OpenResty is enabled                                                                  |
| `7876`  | TCP         | Custodian council communication  | Externally reachable when Custodian is enabled; restrict sources to intended council participants |

When summit-checkpointer is enabled, its RPC and snapshot server binds only to
`127.0.0.1:42069`. Do not expose TCP port `42069` directly through a firewall.
When OpenResty is enabled, it provides the rate-limited and JWT-protected
`/checkpointer` HTTPS route instead.

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

The installer requires Ubuntu's system Python at `/usr/bin/python3`, version
3.12 or newer, with the standard-library `tomllib` module. It validates this
before collecting configuration and includes `python3` in the package plan.

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

The Custodian council listener defaults to `0.0.0.0:7876`. TCP port `7876` must
be reachable from outside the node for key rotation. The generated validator
Custodian program also receives the Summit key directory so it can authenticate
and serve configured observer Custodians. Configure the cloud firewall, security
group, and host firewall as needed, and restrict allowed source addresses to
intended council participants and observer hosts rather than exposing the port
more broadly than necessary. Observer root keys and plaintext epoch-key material
transit the parent-Custodian connection; use a private network or TLS tunnel.

For Summit, seismic-reth, summit-checkpointer, and Centralized Custodian,
supported installation modes are:

1. Install a supplied prebuilt executable.
2. Build from source during installation.
3. Defer installation and provide the executable later.

The current source-build defaults are:

```text
Summit:       main
seismic-reth: feat/purpose-key-rotation-reth
Checkpointer: main
Custodian:    d/centralized-custodian
```

Deferred binaries must be installed at the configured target paths before the
corresponding services are started. A prebuilt or already-present deferred
summit-checkpointer must support `--bind-address`; the generated Supervisor
program uses it to keep the checkpointer RPC on loopback.

### Persistent layout

The default persistent paths are:

| State                             | Default path               |
| --------------------------------- | -------------------------- |
| Reth data                         | `/persistence/reth`        |
| Summit data                       | `/persistence/summit`      |
| Validator keys                    | `/persistence/keys`        |
| Checkpointer output, when enabled | `/persistence/checkpoints` |
| Custodian data, when enabled      | `/persistence/custodian`   |

Important files derived from those paths include:

```text
<persistence keys>/reth/p2p-key
<persistence keys>/summit/node_key.pem
<persistence keys>/summit/consensus_key.pem
```

Back up the validator keys securely. Do not copy them into tickets, chat
messages, source control, or the deposit-signature file described below.

#### Reth MDBX database

seismic-reth stores its MDBX execution database under:

```text
<RETH_DATA_DIR>/db
```

Treat this as persistent execution-layer state. Do not delete it during an
installer rerun, and do not manually copy or modify the live database while Reth
is running.

When summit-checkpointer is enabled, it uses `mdbx_copy` to produce a consistent
Reth database copy. `mdbx_copy` must be built from the same vendored libmdbx
revision as the seismic-reth binary. If Reth is built from source, the installer
builds `mdbx_copy` from that checkout. For a prebuilt or deferred Reth binary,
provide the matching seismic-reth source checkout when prompted. Do not start
the checkpointer until a compatible executable is installed at:

```text
/usr/local/bin/mdbx_copy
```

### Generated services and configuration

The installer writes the Supervisor configuration to:

```text
/etc/supervisor/conf.d/seismic-validator.conf
```

It always defines:

- `summit-deposit-rpc`
- `reth`
- `summit`
- `summit-checkpoint`

It also defines `checkpointer` and `custodian` when those components are
enabled. `summit-checkpoint` is manual-only and reads its checkpoint and
weak-subjectivity paths from:

```text
/etc/seismic/validator-checkpoint-start.toml
```

The installer does not create this runtime file or download a checkpoint. It
installs the checkpoint runner at:

```text
/usr/local/libexec/seismic/summit-checkpoint-runner
```

The checkpoint program fails if the file or required artifacts are missing. The
normal and checkpoint Summit programs use the same process lock and cannot run
simultaneously. Supervisor logs are written under:

```text
/var/log/seismic-validator/
```

After a successful installation, the installer atomically writes the runtime
paths needed by post-install checkpoint tools to:

```text
/etc/seismic/validator-installation.toml
```

The root-owned file contains only the configured Reth and Summit data and key
paths; it contains no key material or other secrets. If it already exists when
the installer starts, the installer asks for permission to overwrite it after a
successful installation. It does not read or reuse the existing contents.
Declining the overwrite cancels the installation before configuration begins.

When summit-checkpointer is enabled, the installer prompts for an absolute
configuration-file path and defaults to:

```text
/etc/seismic/summit-checkpointer.toml
```

The generated Supervisor command uses the selected path. Its parent directory
hierarchy must be root-owned and must not be group- or world-writable.

When OpenResty is enabled, the installer prompts for an absolute JWT-secret file
path and defaults to:

```text
/etc/seismic/openresty-jwt-secret
```

It also writes:

```text
/usr/local/openresty/nginx/conf/nginx.conf
/usr/local/openresty/nginx/lua/rate_limit.lua
/usr/local/openresty/nginx/lua/jwt_auth.lua
/etc/logrotate.d/openresty
```

The generated JWT middleware reads the selected secret path. The secret is
installed as `root:nogroup` with mode `0640` and is not printed by the
installer. Its parent hierarchy must be root-owned, non-writable by group or
others, and traversable by the OpenResty worker user. Reruns reuse a secret
already at the selected path. When changing from the default to a custom path
that does not yet exist, the installer copies the existing default secret
without removing the old file; remove it only after validating the new OpenResty
configuration.

The installer records the selected path in:

```text
/etc/seismic/openresty-jwt-secret.path
```

This root-owned metadata file contains only the path, not the secret. Installer
reruns use it as the JWT-secret prompt default. Generate a one-hour bearer token
from the repository root with:

```bash
TOKEN=$(sudo ./tools/generate-openresty-jwt.sh)
```

The script reads the metadata file, falls back to the standard secret path for
older installations, and prints only the token. Use `--secret-path` to override
the lookup or `--ttl-seconds` to select a lifetime up to 15552000 seconds (180
days). Setting `--ttl-seconds -1` omits the expiration claim; that token remains
valid until the shared JWT secret is rotated, so store and use it as a permanent
credential.

## First validator startup

Use the node tool to generate the fixed deposit-signature request. It loads the
installed Supervisor program definitions itself, so no manual `supervisorctl`
steps are required first:

```bash
sudo ./tools/seismic-node.py validator deposit-signature \
  --output /root/deposit-signature.json
```

Send that file to Seismic operations through a secure channel. After operations
submits the deposit, the following command can download and install a
checkpoint, obtain weak subjectivity from an independent Summit RPC, and
coordinate startup:

```bash
sudo ./tools/seismic-node.py validator onboard \
  --deposit-signature /root/deposit-signature.json \
  --summit-rpc-url https://trusted-validator.example/summit \
  --snapshot-api-url https://snapshot.example/checkpointer \
  --snapshot-bearer-token-file /root/snapshot-token \
  --weak-subjectivity-rpc-url https://independent-validator.example/summit
```

When startup is authorized, `validator onboard` automatically runs:

```bash
sudo systemctl enable --now supervisor
sudo supervisorctl reread
sudo supervisorctl update
```

It then starts Custodian when configured, Reth, `summit-checkpoint`, and
summit-checkpointer when configured. It does not start or reload OpenResty.

For unattended installation, pass `--yes` to skip the interactive confirmation,
and pin `--checkpoint-epoch` with `--checkpoint-policy exact` so a dynamically
selected epoch cannot differ from the one authorized by automation.

Checkpoint installation may complete before the validator becomes `Joining`.
When the account is not yet `Joining`, interactive mode asks whether to wait,
start with a warning, or leave the verified checkpoint installed with every
service stopped. Non-interactive use requires `--pre-joining-policy`.

The detailed manual sequence below remains useful for troubleshooting.

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

Run the following on the validator server while `summit-deposit-rpc` is running:

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

Send the `deposit-signature.json` to Seismic operations through a secure
transfer channel.

Do **not** send validator private-key files, a wallet private key, seed phrases,
or JWT secrets. Do not publish the signature file in a public issue or source
repository.

Seismic operations will submit the staking transaction on your behalf. Record
the transaction hash and confirmation. Starting before the account becomes
`Joining` is possible but may not allow Summit to connect or synchronize yet.

### 7. Restart an onboarded validator

For first-time, lifecycle-gated startup, use `validator onboard` as shown above.
Restart an already onboarded validator from its existing state with:

```bash
sudo ./tools/seismic-node.py validator start --mode normal
```

Use `--mode checkpoint` to restart from the installed checkpoint inputs instead.
The normal and checkpoint Summit programs cannot run at the same time. The
manual normal-mode equivalent starts only the components enabled during
installation, in this order:

```bash
sudo systemctl enable --now supervisor
sudo supervisorctl reread
sudo supervisorctl update

# Run this only when Custodian was configured.
sudo supervisorctl start custodian

sudo supervisorctl start reth
sudo supervisorctl start summit

# Run this only when summit-checkpointer was enabled.
sudo supervisorctl start checkpointer

sudo supervisorctl status
```

Because `autostart=false` and `autorestart=false`, these programs must be
started manually again after a server or Supervisor restart.

## Install a validator checkpoint manually

`validator onboard` downloads and installs a checkpoint automatically. Use this
standalone command when the snapshot archive, its matching `manifest.json`, and
an independently obtained weak-subjectivity TOML were staged locally under
root-owned, non-writable paths — for example on hosts without direct access to
the snapshot provider. Complete the deposit-signature handoff first so the
validator can be started once its account reaches `Joining`.

```bash
sudo ./tools/seismic-node.py checkpoint install \
  --role validator \
  --archive /root/seismic-checkpoint/epoch_12.tar.gz \
  --manifest /root/seismic-checkpoint/manifest.json \
  --weak-subjectivity-path /root/seismic-trust/weak_subjectivity.toml
```

The tool verifies the manifest and archive, checks that related Supervisor
programs are stopped, preserves node keys, backs up and replaces the matching
Reth state, and backs up and empties the Summit mutable store. Reth, Summit, the
checkpoint destination, and the rollback directory must share a filesystem so
state moves remain atomic. The default checkpoint and rollback locations are
derived from the configured data paths. It writes
`/etc/seismic/validator-checkpoint-start.toml` and leaves every service stopped.

The tool prints the root-only rollback directory as soon as it is created and
again in its final summary, together with the exact rollback command. It can
also download a snapshot and obtain weak subjectivity from an independent Summit
RPC; run `sudo ./tools/seismic-node.py checkpoint install --help` for those
options. Independent providers are recommended. If both remote sources share a
normalized URL origin, the tool prints a strong warning and requires interactive
confirmation or `--allow-same-origin-weak-subjectivity` for non-interactive use.
This URL-origin comparison cannot prove that different DNS names have
independent operators. Do not remove the rollback directory until checkpoint
startup and the transition back to normal Summit startup have both been
verified. When rollback is no longer needed, remove it explicitly with:

```bash
sudo ./tools/seismic-node.py checkpoint delete-backup --backup <backup-path>
```

## Stop the validator

Stop deposit-signature, normal, or checkpoint validator programs and their
dependencies in reverse order with:

```bash
sudo ./tools/seismic-node.py validator stop
sudo supervisorctl status
```

The command validates the validator installation inventory before stopping
services. Supervisor and OpenResty remain running.

The complete manual equivalent is:

```bash
sudo supervisorctl stop checkpointer       # when configured
sudo supervisorctl stop summit-deposit-rpc
sudo supervisorctl stop summit
sudo supervisorctl stop summit-checkpoint
sudo supervisorctl stop reth
sudo supervisorctl stop custodian          # when configured
sudo supervisorctl status
```

## OpenResty public endpoint

When enabled, OpenResty terminates HTTPS, obtains certificates through
`lua-resty-auto-ssl`, applies per-client rate limiting, and proxies local Reth,
Summit, and summit-checkpointer endpoints.

Reth HTTP and WebSocket RPC, Summit RPC, their metrics listeners, and the
summit-checkpointer RPC remain bound to loopback whether or not OpenResty is
enabled. When OpenResty is disabled, these endpoints are available only from the
node itself or through an operator-managed tunnel; they are not exposed directly
on public interfaces.

The configured routes are:

| Public path     | Local upstream          | Notes                                            |
| --------------- | ----------------------- | ------------------------------------------------ |
| `/`             | `127.0.0.1:3000`        | Grafana                                          |
| `/staking`      | `/var/www/html/staking` | Static staking UI                                |
| `/rpc`          | `127.0.0.1:8545`        | Reth HTTP JSON-RPC                               |
| `/ws`           | `127.0.0.1:8546`        | Reth WebSocket JSON-RPC                          |
| `/summit`       | `127.0.0.1:3030`        | Summit RPC                                       |
| `/ops`          | `127.0.0.1:8552`        | Signature-authenticated privileged Reth RPC      |
| `/checkpointer` | `127.0.0.1:42069`       | Rate-limited and JWT-protected RPC and snapshots |
| `/prom-summit`  | `127.0.0.1:9090`        | Rate-limited and JWT-protected                   |
| `/prom-reth`    | `127.0.0.1:9001`        | Rate-limited and JWT-protected                   |

The `/checkpointer` route uses the same secret at the selected JWT-secret path
as the protected metrics routes. The default path is
`/etc/seismic/openresty-jwt-secret`. Clients must send
`Authorization: Bearer <token>`. The localhost-only `summit-deposit-rpc` service
on port `3031` remains deliberately unproxied.

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

For source installations, new checkouts fetch all remote branches. On a rerun,
the installer validates the origin and clean working tree, configures `origin`
to fetch all branches, fetches and prunes remote references, checks out or
creates the configured local branch, and merges `origin/<branch>` with
`--ff-only` before rebuilding. Dirty, diverged, or force-pushed checkouts are
rejected rather than reset. The installer does not update its own
`seismic-node-ops` checkout.

Before applying a later `supervisorctl update`, inspect running services:

```bash
sudo supervisorctl status
```

Supervisor may restart a running program whose definition changed when the
updated configuration is applied. Plan that operation separately for a live
validator.
