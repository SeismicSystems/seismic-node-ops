# Seismic Observer Installer and First-Start Guide

This directory contains the interactive installer for a Seismic observer node:

```text
install/install-observer.sh
```

The installer prepares the observer software, persistent directories, keys,
Supervisor programs, and optional OpenResty endpoint. It deliberately does not
start the observer services. Follow the first-start procedure in this document
after installation.

For a validator node, use `install/install-validator.sh` and follow the separate
**[Validator Installer and First-Start Guide](README.md)**.

## Safety model

The installer is designed to avoid replacing persistent observer state:

- Observer services run as an existing non-root service user.
- Persistent directories must be absolute, non-root, and non-overlapping.
- Existing Reth P2P keys are validated and preserved.
- Existing observer consensus and parent node keys are preserved only when the
  assignment marker matches the configured parent public key and observer index.
- Keys without an assignment marker, or a marker for another assignment, cause
  the installer to stop rather than reuse or replace them.
- Existing observer Custodian root keys are preserved for verification against
  the parent Custodian.
- Service binaries are installed as root-owned, non-writable executables.
- Supervisor programs use `autostart=false` and `autorestart=false`.
- The installer does not start, enable, reread, update, reload, or restart
  observer services.

Review the complete interactive configuration summary before accepting it.

## Requirements

- Ubuntu 24.04 LTS (Noble) with `apt-get`.
- Root access through `sudo`.
- An existing non-root Linux user that will run the observer services.
- Python 3 for public Summit socket-address validation.
- The Summit genesis TOML for the selected network; it must remain readable by
  the service user.
- The parent validator's Summit node public key and the selected observer index.
- A literal public IPv4 or bracketed IPv6 Summit P2P socket address for the
  observer. Summit's default P2P port is `18551`.
- The parent validator's private Summit `node_key.pem`, which must be placed at
  the configured observer Summit key path after installation.
- Sufficient persistent storage for Reth, Summit, observer keys, and any enabled
  optional components.
- Network access to package and source repositories when installing packages or
  building from source.
- When Custodian is enabled, the reachable council endpoint of the Custodian
  running on the parent validator, normally `PARENT_IP:7876`.
- If configuring OpenResty, DNS for the selected domain must point to the node.

The installer validates local input and key consistency. It deliberately does
not check whether the parent key is a genesis validator or a current validator.
Network authorization is outside the installation process.

The installation log is written to:

```text
/var/log/seismic-observer-install.log
```

### Network and firewall requirements

The installer does not configure cloud firewall, security-group, or host
firewall rules. Configure the required access before starting the observer.

| Port                       | Protocol    | Purpose                             | Required exposure                                                                         |
| -------------------------- | ----------- | ----------------------------------- | ----------------------------------------------------------------------------------------- |
| `30303`                    | TCP and UDP | seismic-reth P2P and discovery      | Public                                                                                    |
| `18551` or configured port | TCP and UDP | Summit observer P2P                 | Public                                                                                    |
| `80`                       | TCP         | HTTP redirect and ACME challenge    | Public when OpenResty is enabled                                                          |
| `443`                      | TCP         | OpenResty HTTPS endpoint            | Public when OpenResty is enabled                                                          |
| `7876` or configured port  | TCP         | Observer Custodian council listener | Externally reachable when Custodian is enabled; restrict sources to intended participants |

When observer Custodian is enabled, the observer also needs outbound TCP access
to the configured parent validator Custodian council endpoint. The parent
firewall must allow the observer's source IP.

The parent-Custodian protocol transports root-key and plaintext epoch-key
material. Use a private network or protect the path with a TLS tunnel. Do not
send it over an untrusted plaintext network.

The following application ports must remain loopback-only:

```text
3000 3030 3031 8545 8546 8552 8999 9001 9090 42069
```

When summit-checkpointer is enabled, its direct listener remains on
`127.0.0.1:42069`. When OpenResty is enabled, remote clients use the
rate-limited and JWT-protected `/checkpointer` HTTPS route instead of direct
access to that port.

### Supplied internal-testnet genesis

The repository includes the Summit genesis file for the internal testnet:

```text
internal_testnet_genesis.toml
```

When the installer asks for the Summit genesis TOML path, enter its absolute
path.

Keep this file in place after installation. The generated Supervisor program
references the selected path directly, and the configured service user must be
able to read it.

The installer requires Ubuntu's system Python at `/usr/bin/python3`, version
3.12 or newer, with the standard-library `tomllib` module. It validates this
before collecting configuration and includes `python3` in the package plan.

## Run the installer

Run the installer from the repository root:

```bash
sudo ./install/install-observer.sh
```

The installer asks you to configure:

- The non-root service user.
- Persistent Reth, Summit, and observer-key directories.
- An optional public HTTPS endpoint through OpenResty.
- The Summit genesis file and an optional Reth bootnode RPC.
- The parent validator Summit node public key.
- An observer derivation index from `0` through `255`.
- The observer's public Summit P2P `IP:port`; the default Summit port is
  `18551`.
- An optional path to a Summit bootstrappers TOML file.
- Summit and seismic-reth binary installation methods.
- summit-checkpointer.
- Centralized Custodian, including the parent validator Custodian council
  endpoint and Custodian binary installation method.

Neither the parent nor nodes listed in an optional bootstrappers file are
required to appear in the genesis `[[validators]]` list.

### Binary installation methods

Summit, seismic-reth, summit-checkpointer, and Centralized Custodian support:

1. Install a supplied prebuilt executable.
2. Build from source during installation.
3. Defer installation and provide the executable later.

The current source-build defaults are:

```text
Summit:       m/metrics
seismic-reth: feat/purpose-key-rotation-reth
Checkpointer: main
Custodian:    d/centralized-custodian
```

A prebuilt or already-present deferred summit-checkpointer must support
`--bind-address`; the generated Supervisor program uses it to keep the
checkpointer RPC and snapshot server on `127.0.0.1:42069`.

A prebuilt or already-present deferred observer Custodian is checked for these
required options:

```text
--summit-key-dir
--observer
--parent-custodian
```

Deferred binaries must be installed at their configured target paths before the
corresponding services are started.

### Optional Summit bootstrappers file

A bootstrappers file can contain one or more known Summit peers:

```toml
[[bootstrappers]]
node_public_key = "32-byte Summit node public key"
address = "203.0.113.10:18551"

[[bootstrappers]]
node_public_key = "another 32-byte Summit node public key"
address = "198.51.100.20:18551"
```

The selected source path must be absolute and point to a non-empty,
non-symbolic-link file readable by the service user. The installer copies it to
the configured Summit key directory as `bootstrappers.toml`; Supervisor
references that stable copy.

This file configures Summit consensus peers. It is separate from the optional
Reth bootnode RPC used to discover an execution-layer enode.

## Persistent layout

The default persistent paths are:

| State                             | Default path               |
| --------------------------------- | -------------------------- |
| Reth data                         | `/persistence/reth`        |
| Summit observer data              | `/persistence/summit`      |
| Observer and Reth keys            | `/persistence/keys`        |
| Checkpointer output, when enabled | `/persistence/checkpoints` |
| Custodian data, when enabled      | `/persistence/custodian`   |

Important files derived from those paths include:

```text
/persistence/keys/reth/p2p-key
/persistence/keys/summit/observer-assignment
/persistence/keys/summit/consensus_key.pem
/persistence/keys/summit/node_key.pem
/persistence/keys/summit/bootstrappers.toml
/persistence/custodian/root.key
```

Every persistent directory is configurable. Changing a directory on a later
installer run does not migrate existing state; it selects a separate store.

### Observer assignment and keys

The observer assignment is represented as:

```text
<parent-node-public-key>:<observer-index>
```

and stored in:

```text
<persistent keys>/summit/observer-assignment
```

The installer generates the observer's independent `consensus_key.pem`. It does
not generate or copy the parent validator's private `node_key.pem`.

Before starting Custodian or `summit-observer`, place the parent validator's
private node key at the configured Summit key path. With the default paths, the
required destination is:

```text
/persistence/keys/summit/node_key.pem
```

The file must be owned by the configured service user and have mode `0600`. The
Summit key directory must have mode `0700`. The private key must correspond to
the parent public key entered during installation.

Summit derives the observer's secondary P2P identity from the parent node key
and observer index. The observer's separate consensus key does not make it a
voting or proposing validator.

### Reth MDBX database

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

## Generated services and configuration

The observer installer writes the Supervisor configuration to:

```text
/etc/supervisor/conf.d/seismic-observer.conf
```

It always defines:

- `reth`
- `summit-observer`
- `summit-observer-checkpoint`

It also defines `checkpointer` and `custodian` when those components are
enabled. It does not define `summit-deposit-rpc`; observers do not create
validator deposit signatures.

`summit-observer-checkpoint` is manual-only and reads its checkpoint and
weak-subjectivity paths from:

```text
/etc/seismic/observer-checkpoint-start.toml
```

The installer does not create this runtime file or download a checkpoint. It
installs the checkpoint runner at:

```text
/usr/local/libexec/seismic/summit-checkpoint-runner
```

The checkpoint program fails if the file or required artifacts are missing. The
normal and checkpoint observer programs use the same process lock and cannot run
simultaneously.

Supervisor logs are written under:

```text
/var/log/seismic-observer/
```

After a successful installation, the installer atomically writes the runtime
paths and observer assignment needed by post-install checkpoint tools to:

```text
/etc/seismic/observer-installation.toml
```

The root-owned file contains no private keys or other secrets. If it already
exists when the installer starts, the installer asks for permission to overwrite
it after a successful installation. It does not read or reuse the existing
contents. Declining the overwrite cancels the installation before configuration
begins.

## Install an observer checkpoint

Stage the snapshot archive, its matching `manifest.json`, and an independently
obtained weak-subjectivity TOML under root-owned, non-writable paths. Then run:

```bash
sudo ./tools/seismic-node.py checkpoint install \
  --role observer \
  --archive /root/seismic-checkpoint/epoch_12.tar.gz \
  --manifest /root/seismic-checkpoint/manifest.json \
  --weak-subjectivity-path /root/seismic-trust/weak_subjectivity.toml
```

The tool reads `/etc/seismic/observer-installation.toml`, verifies the observer
assignment and preserved node keys, checks that related Supervisor programs are
stopped, restores matching Reth state, and backs up and empties the Summit
mutable store. Reth, Summit, the checkpoint destination, and the rollback
directory must share a filesystem so state moves remain atomic. It writes
`/etc/seismic/observer-checkpoint-start.toml` and leaves every service stopped.

The tool prints the root-only rollback directory and exact rollback command. It
can also download the snapshot and obtain weak subjectivity from an independent
Summit RPC. Independent providers are recommended. If both remote sources share
a normalized URL origin, the tool prints a strong warning and requires
interactive confirmation or `--allow-same-origin-weak-subjectivity` for
non-interactive use. This comparison cannot prove that different DNS names have
independent operators. Do not remove the backup until checkpoint startup and the
transition back to normal observer startup have both been verified.

To download, install, and start in one command, run:

```bash
sudo ./tools/seismic-node.py observer start \
  --mode checkpoint \
  --summit-rpc-url https://trusted-validator.example/summit \
  --snapshot-api-url https://snapshot.example/checkpointer \
  --snapshot-bearer-token-file /root/snapshot-token \
  --weak-subjectivity-rpc-url https://independent-validator.example/summit
```

For unattended installation, pass `--yes` to skip the interactive confirmation,
and pin `--checkpoint-epoch` with `--checkpoint-policy exact` so a dynamically
selected epoch cannot differ from the one authorized by automation.

To start from an already installed checkpoint, omit the snapshot and
weak-subjectivity source options. Normal startup uses `--mode normal`, including
a later restart without checkpoint arguments. The checkpoint Supervisor program
must be stopped first; the CLI refuses to run the normal and checkpoint programs
together. When rollback is no longer needed, remove it explicitly with:

```bash
sudo ./tools/seismic-node.py checkpoint delete-backup --backup <backup-path>
```

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

Reth HTTP, WebSocket, Ops, and metrics endpoints and Summit RPC, admin RPC, and
metrics endpoints bind to loopback whether or not OpenResty is enabled.

## Observer Custodian

The parent validator Custodian council endpoint and the observer's local council
listener are different settings:

- The parent endpoint is a remote `host:port` that the observer contacts to
  fetch or verify the root key and synchronize epoch-key deliveries.
- The local listener is the observer Custodian's own bind address and defaults
  to `0.0.0.0:7876`.

The generated observer Custodian command includes:

```text
--observer <observer-index>
--parent-custodian <parent-host:port>
--summit-key-dir <observer-summit-key-directory>
```

If the configured Custodian `root.key` is absent, the observer Custodian fetches
it from the parent on first startup. If it already exists, the installer
preserves it and the Custodian verifies it against the parent. Installer reruns
do not replace an existing observer root key.

The parent validator Custodian must be running with its own `--summit-key-dir`
argument so it can authenticate and serve observer requests.

## Start the observer

Do not start Custodian or Summit until the parent validator's private
`node_key.pem` exists at the configured observer Summit key path.

### Coordinated start

Use the node tool for normal startup:

```bash
sudo ./tools/seismic-node.py observer start --mode normal
```

The command validates the observer installation inventory and then runs:

```bash
sudo systemctl enable --now supervisor
sudo supervisorctl reread
sudo supervisorctl update
```

It starts configured Supervisor programs in dependency order: Custodian when
enabled, Reth, `summit-observer`, and summit-checkpointer when enabled. If a
program started by the command fails, it stops only the programs that invocation
started, in reverse order.

The command does not start or reload OpenResty. Start OpenResty separately as
described under [OpenResty public endpoint](#openresty-public-endpoint).

### Manual start and troubleshooting

The complete manual normal-mode equivalent is:

```bash
sudo systemctl enable --now supervisor
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start custodian       # when configured
sudo supervisorctl start reth
sudo supervisorctl start summit-observer
sudo supervisorctl start checkpointer    # when configured
sudo supervisorctl status
```

For checkpoint mode, start `summit-observer-checkpoint` instead of
`summit-observer`. The normal and checkpoint programs must not run together. The
following sections provide individual checks and troubleshooting commands.

#### Start Custodian when enabled

Custodian must start before Reth because Reth connects to its Unix socket during
startup:

```bash
sudo supervisorctl start custodian
sudo supervisorctl status custodian
```

With default paths, verify its state and socket:

```bash
sudo stat /persistence/custodian/root.key
sudo test -S /tmp/custodian.sock
```

If it does not start, inspect:

```bash
sudo tail -n 100 /var/log/seismic-observer/custodian.err
sudo tail -n 100 /var/log/seismic-observer/custodian.log
```

Skip this step when Custodian was not enabled.

#### Start Reth

```bash
sudo supervisorctl start reth
sudo supervisorctl status reth
```

If it does not start, inspect:

```bash
sudo tail -n 100 /var/log/seismic-observer/reth.err
sudo tail -n 100 /var/log/seismic-observer/reth.log
```

Reth must be running before Summit because Summit connects through the Engine
API IPC socket.

#### Start Summit observer

```bash
sudo supervisorctl start summit-observer
sudo supervisorctl status summit-observer
```

If it does not start, inspect:

```bash
sudo tail -n 100 /var/log/seismic-observer/summit-observer.err
sudo tail -n 100 /var/log/seismic-observer/summit-observer.log
```

The dependency order is:

```text
custodian -> reth -> summit-observer
```

When Custodian is disabled, start only Reth and Summit observer in that order.

#### Start summit-checkpointer when enabled

```bash
sudo supervisorctl start checkpointer
sudo supervisorctl status checkpointer
```

If it does not start, inspect:

```bash
sudo tail -n 100 /var/log/seismic-observer/checkpointer.err
sudo tail -n 100 /var/log/seismic-observer/checkpointer.log
```

summit-checkpointer produces checkpoint artifacts. Starting a validator or
observer from a checkpoint remains the responsibility of the dedicated
checkpoint-start CLI.

Because `autostart=false` and `autorestart=false`, all enabled programs must be
started manually again after a server or Supervisor restart.

## Stop the observer

Use the node tool to stop both possible Summit observer modes and all configured
dependencies in reverse order:

```bash
sudo ./tools/seismic-node.py observer stop
sudo supervisorctl status
```

The command validates the observer installation inventory, then stops
summit-checkpointer when configured, `summit-observer`,
`summit-observer-checkpoint`, Reth, and Custodian when configured. It stops
lower-level dependencies only after the programs that depend on them have
stopped successfully.

Supervisor and OpenResty remain running. Stop OpenResty separately only when the
public endpoint should also be taken offline.

### Manual stop

The complete manual equivalent is:

```bash
sudo supervisorctl stop checkpointer                 # when configured
sudo supervisorctl stop summit-observer
sudo supervisorctl stop summit-observer-checkpoint
sudo supervisorctl stop reth
sudo supervisorctl stop custodian                    # when configured
sudo supervisorctl status
```

## OpenResty public endpoint

When enabled, OpenResty terminates HTTPS, obtains certificates through
`lua-resty-auto-ssl`, applies per-client rate limiting, and proxies local Reth,
Summit, and summit-checkpointer endpoints.

Reth HTTP and WebSocket RPC, Reth Ops RPC, Summit RPC, metrics listeners, and
the summit-checkpointer RPC remain bound to loopback whether or not OpenResty is
enabled. When OpenResty is disabled, these endpoints are available only from the
node itself or through an operator-managed tunnel.

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
`Authorization: Bearer <token>`. The Summit admin RPC on loopback port `3031`
remains deliberately unproxied.

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

## Verify the observer

Check service state:

```bash
sudo supervisorctl status
```

Inspect listening sockets:

```bash
sudo ss -lntup
```

Confirm that:

- Reth P2P is reachable on configured TCP and UDP port `30303`.
- Summit P2P is reachable on its configured public address, normally port
  `18551`.
- Reth ports `8545`, `8546`, `8552`, and `9001` are loopback-only.
- Summit ports `3030`, `3031`, and `9090` are loopback-only.
- summit-checkpointer port `42069` is loopback-only when enabled.
- Custodian listens on the configured council address when enabled.
- OpenResty listens on ports `80` and `443` only when configured and explicitly
  started.

Follow the main logs:

```bash
sudo tail -f \
  /var/log/seismic-observer/reth.log \
  /var/log/seismic-observer/summit-observer.log
```

There is no staking transaction or deposit-signature workflow for an observer.

## Safe installer reruns

The installer can be rerun to update binaries or rendered configuration, but it
does not persist previous answers. Re-enter the same service user, persistent
directories, genesis path, observer assignment, and optional component settings
unless you are intentionally changing them.

On a normal rerun, the installer:

- Preserves the Reth P2P key.
- Preserves Summit keys when the assignment marker matches.
- Refuses to reuse keys for another parent or observer index.
- Preserves an existing observer Custodian root key.
- Replaces the installer-managed bootstrappers file from the accepted source, or
  removes it when no source is configured.
- Replaces generated OpenResty and Supervisor configuration.
- Leaves services stopped and does not reload Supervisor or OpenResty.

Changing a persistent path does not migrate existing data. It creates or uses a
separate store.

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
observer.

## Troubleshooting

### Parent node key is missing or does not match

Confirm that `node_key.pem` exists at the configured Summit key path, is owned
by the service user, and has mode `0600`. Run:

```bash
sudo -u SERVICE_USER /usr/local/bin/summit keys show \
  --key-store-path /path/to/configured/summit/keys
```

The displayed node public key must match the parent public key accepted by the
installer.

### Observer assignment marker mismatch

The marker must contain exactly:

```text
<parent-node-public-key>:<observer-index>
```

Do not edit it to repurpose existing keys or state. Use separate persistent key
and Summit data paths for a different assignment.

### Parent Custodian is unreachable

Verify the configured parent `host:port`, outbound routing, and the parent
firewall. The parent Custodian must be running and configured with its Summit
key directory.

### Custodian root-key fetch or verification fails

Check that the observer index, parent node key, chain ID, council address, and
parent Custodian endpoint all match the intended deployment. Do not delete an
existing root key merely to bypass a mismatch.

### Custodian socket is missing

Inspect the Custodian logs and confirm that the configured socket parent path is
usable. Reth must not be started in Custodian mode until the Unix socket exists.

### Reth cannot start

Check the Reth executable, Reth P2P key, data-directory ownership, Custodian
socket when enabled, and Reth error log. Do not delete the MDBX database as a
first troubleshooting step.

### Summit cannot start

Check the Summit executable, genesis readability, assignment marker, both Summit
key files, Engine API IPC availability, public P2P address, and Summit error
log.

### Observer cannot find Summit peers

Confirm that the genesis contains reachable initial peers or provide a valid
bootstrappers TOML. The bootstrappers file must contain the expected public keys
and reachable `IP:port` addresses.

### RPC or metrics port is publicly exposed

Stop the affected program and inspect:

```text
/etc/supervisor/conf.d/seismic-observer.conf
```

Reth HTTP, WebSocket, Ops, and metrics and Summit RPC, admin RPC, and metrics
must bind to loopback. Only P2P and explicitly configured public services should
bind externally.

### Deferred binary is missing or unsafe

Install the executable at the accepted target path as a root-owned mode `0755`
regular file. Parent directories must also be root-owned and not writable by the
service user.

### OpenResty validation fails

Run:

```bash
sudo openresty -t
```

Then inspect:

```text
/var/log/seismic-observer-install.log
/usr/local/openresty/nginx/conf/nginx.conf
```

Do not start or reload OpenResty until its configuration test passes.
