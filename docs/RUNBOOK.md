# Seismic node runbook

This document describes, step by step, what a Seismic validator or observer host
looks like and how to bring one up, operate it, and change it. Every file the
steps create is either shipped verbatim in this repository or derived from one
operator-authored file, `/etc/seismic/node.env`.

Role-specific steps are marked **validator** or **observer**. Unmarked steps
apply to both.

> **Installer.** `install/install.sh --config node.env` performs section 3 from
> a prepared `node.env` with no prompts; each step below names the installer
> step that implements it. Operators who prefer configuration management can
> follow the steps directly (section 8).

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Host layout](#2-host-layout)
3. [Installation](#3-installation)
4. [First start](#4-first-start)
5. [Operations](#5-operations)
6. [Changing the configuration](#6-changing-the-configuration)
7. [Running as a different account](#7-running-as-a-different-account)
8. [Automating with configuration management](#8-automating-with-configuration-management)

## 1. Prerequisites

### Host

- Ubuntu 24.04 LTS with systemd (the default).
- Root access through `sudo`.
- Python 3.12 or newer at `/usr/bin/python3` (the Ubuntu system Python).
- Persistent storage on one filesystem for Reth, Summit, keys, and any optional
  components. Checkpoint installation moves state with `rename(2)` and requires
  this.
- Outbound network access to the Seismic release artifacts (or source
  repositories) and, when monitoring is enabled, to the central Prometheus.
- For `download` installs, the GitHub CLI (`gh`) 2.49 or newer to verify release
  attestations. Ubuntu 24.04's own `gh` package is 2.45 and lacks
  `gh attestation`, so the installer adds GitHub's apt repository (signing key
  pinned by fingerprint) and installs `gh` from there. No GitHub account or
  login is required when the attestation bundle is downloaded from the release
  and passed with `--bundle`. The bare `gh attestation verify` form, by
  contrast, fetches the attestation through GitHub's API and requires
  `gh auth login`.

### Inbound ports

Configure cloud and host firewalls before the first start.

| Port    | Protocol  | Purpose                                     | Exposure                           |
| ------- | --------- | ------------------------------------------- | ---------------------------------- |
| `30303` | TCP + UDP | seismic-reth P2P and discovery              | Public                             |
| `18551` | TCP + UDP | Summit consensus P2P                        | Public                             |
| `80`    | TCP       | ACME challenge and HTTP redirect            | Public when OpenResty is managed   |
| `443`   | TCP       | HTTPS endpoint                              | Public when any terminator is used |
| `7876`  | TCP       | Custodian council backend (configurable)    | Loopback only; never expose        |
| `42069` | TCP       | seismic-checkpointer snapshot server        | Loopback only; never expose        |
| `3030`  | TCP       | Summit RPC                                  | Loopback only                      |
| `8545`  | TCP       | Reth HTTP RPC (`8546` WebSocket)            | Loopback only                      |
| `9001`  | TCP       | Reth Prometheus metrics (`9090` for Summit) | Loopback only                      |

### Inputs to have in hand

| Input                                 | Where it comes from                                | Needed when                                                   |
| ------------------------------------- | -------------------------------------------------- | ------------------------------------------------------------- |
| Genesis TOML                          | `internal_testnet_genesis.toml` in this repository | Always                                                        |
| Bootstrappers TOML                    | Seismic operations; see [3.7 Keys](#37-keys)       | Joining a running network with stale genesis peers            |
| Bootnode RPC URL                      | Seismic operations                                 | Optional; resolves the Reth bootnode enode                    |
| Parent node public key + index        | The parent validator's operator                    | **observer**                                                  |
| Parent Custodian HTTPS base URL       | The parent validator's operator                    | **observer**                                                  |
| EVM chain ID                          | Seismic operations                                 | Always (Custodian)                                            |
| Prometheus Agent token (64 hex chars) | Seismic operations, delivered to a root-only file  | **validator** with `MONITORING_MODE=push`, its default (3.12) |
| Public DNS name                       | You; must resolve to this host                     | Managed OpenResty                                             |

## 2. Host layout

This is the contract every step below establishes. All paths except those under
`/persistence` are fixed; the `/persistence` paths are the defaults in
`node.env` and may be changed there.

### Accounts

| Account              | Purpose                                          | Created by                        |
| -------------------- | ------------------------------------------------ | --------------------------------- |
| `seismic`            | Runs Reth, Summit, Custodian, checkpointer       | Step 3.2                          |
| `seismic-prometheus` | Runs the Prometheus Agent; cannot read node keys | `seismic-node monitoring install` |

### Configuration (`/etc/seismic`, root-owned)

| Path                                     | Mode   | Purpose                                                                                     |
| ---------------------------------------- | ------ | ------------------------------------------------------------------------------------------- |
| `/etc/seismic/node.env`                  | `0640` | **The** node configuration. Read by systemd (`EnvironmentFile=`) and by `seismic-node`.     |
| `/etc/seismic/genesis.toml`              | `0644` | Summit genesis. Identical on every host; its digest gates P2P authentication.               |
| `/etc/seismic/seismic-checkpointer.toml` | `0644` | Rendered from `node.env` when the checkpointer is enabled.                                  |
| `/etc/seismic/checkpoint-start.toml`     | `0644` | Written by `seismic-node checkpoint install`. Its presence allows the `*-checkpoint` units. |
| `/etc/seismic/weak-subjectivity.toml`    | `0644` | Trust anchor named by the above; obtained from an independent source.                       |
| `/etc/seismic/prometheus-agent/`         | `0750` | Agent config and token, group `seismic-prometheus`.                                         |
| `/etc/seismic/openresty-jwt-secret`      | `0640` | `root:nogroup`; JWT secret for full mode or Custodian-only with pull monitoring.            |

### Services (`/etc/systemd/system`, shipped verbatim from `install/templates/systemd/`)

| Unit                                  | Role      | Restart    | Notes                                                                           |
| ------------------------------------- | --------- | ---------- | ------------------------------------------------------------------------------- |
| `seismic-reth.service`                | both      | on-failure | See [Restart policy](#restart-policy)                                           |
| `seismic-summit.service`              | validator | on-failure | Not enabled at boot; see [Restart policy](#restart-policy)                      |
| `seismic-summit-checkpoint.service`   | validator | on-failure | Requires `/etc/seismic/checkpoint-start.toml`                                   |
| `seismic-deposit-rpc.service`         | validator | no         | Started briefly by `seismic-node validator deposit-signature`                   |
| `seismic-custodian.service`           | validator | on-failure | Always installed; serves purpose keys to Reth                                   |
| `seismic-observer.service`            | observer  | on-failure |                                                                                 |
| `seismic-observer-checkpoint.service` | observer  | on-failure | Requires `/etc/seismic/checkpoint-start.toml`                                   |
| `seismic-observer-custodian.service`  | observer  | on-failure | Always installed; fetches root key from parent                                  |
| `seismic-checkpointer.service`        | both      | on-failure | Only when `CHECKPOINTER_ENABLED=true`                                           |
| `seismic-prometheus-agent.service`    | both      | always     | Only when `MONITORING_MODE=push`: the validator default, optional for observers |

Each unit has a drop-in `<unit>.d/10-hardening.conf` from
`install/templates/systemd/seismic-hardening.conf`. No unit is enabled at boot
by default; see [Enabling at boot](#enabling-at-boot).

#### Restart policy

Every long-running unit restarts on failure. This is safe for a voting process
because Summit's consensus engine writes every vote it signs to a journal under
`SUMMIT_DATA_DIR`, fsyncs it **before** broadcasting, and replays it on start.
Therefore, a restarted validator cannot sign a conflicting vote for a view it
already voted in.

Summit depends on Reth's Engine API socket and fails whenever Reth fails. The
Summit units therefore declare `Requires=seismic-reth.service` and
`After=seismic-reth.service`: when Summit restarts, systemd starts Reth first,
so a Reth crash recovers the whole chain in order. The reverse is deliberately
not wired (`PartOf=` is not used), but `Requires=` already propagates an
explicit `systemctl restart seismic-reth` to Summit: systemd queues a restart
job for Summit, so it gets a new process immediately (no `RestartSec` wait, no
failed state, `NRestarts` unchanged), ordered after Reth is up again.

Automatic restart is distinct from starting at boot; see
[Enabling at boot](#enabling-at-boot).

### Binaries (fixed paths named by the units)

| Path                                                         | Component                      |
| ------------------------------------------------------------ | ------------------------------ |
| `/usr/local/bin/seismic-reth`                                | Execution client               |
| `/usr/local/bin/summit`                                      | Consensus client               |
| `/usr/local/bin/seismic-centralized-custodian-service`       | Custodian                      |
| `/usr/local/bin/seismic-checkpointer`                        | Checkpointer (optional)        |
| `/usr/local/bin/mdbx_copy`                                   | Checkpointer helper (optional) |
| `/usr/local/libexec/seismic/summit-checkpoint-runner`        | Checkpoint-start validator     |
| `/usr/local/lib/seismic/prometheus-agent/current/prometheus` | Prometheus (optional)          |

### State (owner `seismic:seismic`)

| Default path                        | Mode   | Contents                                                                                                                                           |
| ----------------------------------- | ------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `/persistence/reth`                 | `0750` | Reth MDBX database (`db/`) and static files                                                                                                        |
| `/persistence/summit`               | `0750` | Summit consensus store; `critical/` log files                                                                                                      |
| `/persistence/keys`                 | `0700` | `reth/p2p-key`, `summit/node_key.pem`, `summit/consensus_key.pem`, optional `summit/bootstrappers.toml`, **observer** `summit/observer-assignment` |
| `/persistence/custodian`            | `0700` | `root.key` (validator), `deliveries/`                                                                                                              |
| `/persistence/checkpoints`          | `0750` | Checkpointer snapshots and state                                                                                                                   |
| `/var/lib/seismic-prometheus-agent` | `0750` | Agent WAL (owner `seismic-prometheus`)                                                                                                             |
| `/run/seismic`                      | `0750` | Engine API and Custodian sockets (tmpfs, recreated)                                                                                                |

All node services log to stdout and stderr, which systemd captures in the
journal; none writes a log file of its own, with one deliberate exception.
Summit additionally writes its most serious errors (fatal database errors,
engine client failures — tracing target `critical`) as rotating JSON files to
`$SUMMIT_DATA_DIR/critical/` (100 MB × 5, rotated by Summit itself), so an
incident record survives journal retention and can be collected without
`journalctl`. The same events are in the journal and increment the
`critical_errors_total` metric for alerting. (The managed OpenResty keeps its
own access and error logs under `/usr/local/openresty/nginx/logs/`, rotated by
the `logrotate` rule the `tls` step installs from
`install/templates/openresty/logrotate-openresty`.)

```bash
journalctl -u seismic-reth -f
journalctl -u seismic-summit -p err
ls -l /persistence/summit/critical/
```

## 3. Installation

`REPO` is a checkout of this repository: the unit files, `node.env.example`, the
genesis file, the checkpoint runner, and the `seismic-node` CLI are taken from
it. The services do not need it at runtime, but the CLI is run from it during
operations, so keep it on the host.

```bash
REPO=/opt/seismic-node-ops
sudo git clone https://github.com/SeismicSystems/seismic-node-ops.git "$REPO"
```

### 3.1 Packages

_Installer step: `packages`._

```bash
sudo apt-get update
sudo apt-get install -y python3 curl jq ca-certificates openssl gettext-base
```

For `download` installs, add GitHub's apt repository and install `gh` from it
(Ubuntu's `gh` is too old; see Prerequisites). The installer checks that the
keyring contains GitHub CLI signing key
`7F38 BBB5 9D06 4DBC B3D8 4D72 5612 B364 6231 3325` before trusting it:

```bash
sudo install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
gpg --show-keys --with-fingerprint /etc/apt/keyrings/githubcli-archive-keyring.gpg   # must list 7F38 ... 3325
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
sudo apt-get update && sudo apt-get install -y gh
gh attestation --help >/dev/null   # confirms a new enough gh
```

Add only when building from source:
`git build-essential clang pkg-config libssl-dev` plus a Rust toolchain for the
`seismic` account (`rustup`). Add only for managed OpenResty:
`gnupg lsb-release luarocks build-essential git`.

### 3.2 Service account

_Installer step: `account`._

```bash
sudo useradd --system --home /var/lib/seismic --shell /usr/sbin/nologin \
        --user-group seismic
```

### 3.3 Directories

_Installer step: `directories`._

Using the defaults from `node.env.example`; adjust if you change the paths.

```bash
sudo install -d -o root    -g root    -m 0755 /etc/seismic /usr/local/libexec/seismic
sudo install -d -o seismic -g seismic -m 0750 /persistence/reth /persistence/summit
sudo install -d -o seismic -g seismic -m 0700 /persistence/keys /persistence/keys/reth /persistence/keys/summit
sudo install -d -o seismic -g seismic -m 0700 /persistence/custodian /persistence/custodian/deliveries
# optional
sudo install -d -o seismic -g seismic -m 0750 /persistence/checkpoints
```

### 3.4 Binaries

_Installer step: `binaries`._

Three binaries are required on every node and two more when the checkpointer is
enabled. The versions are the `*_VERSION` keys in `node.env`. The values in
`node.env.example` are the set released and tested together; they move together,
and a component from a different release is untested against the others. The
installer checks nothing about compatibility between components: `node.env`
records what you chose. The one exception is `mdbx_copy` (below), whose libmdbx
must match Reth's.

| Binary                                                 | Repository                             | Build                                                                     |
| ------------------------------------------------------ | -------------------------------------- | ------------------------------------------------------------------------- |
| `/usr/local/bin/summit`                                | `SeismicSystems/summit`                | `cargo build --locked --release --features prom`                          |
| `/usr/local/bin/seismic-reth`                          | `SeismicSystems/seismic-reth`          | `cargo build --locked --profile maxperf --bin seismic-reth`               |
| `/usr/local/bin/seismic-centralized-custodian-service` | `SeismicSystems/enclave`               | `cargo build --locked --release -p seismic-centralized-custodian-service` |
| `/usr/local/bin/seismic-checkpointer` (optional)       | `SeismicSystems/seismic-checkpointer`  | `cargo build --locked --release`                                          |
| `/usr/local/bin/mdbx_copy` (optional)                  | vendored libmdbx inside `seismic-reth` | `make mdbx_copy` — see below                                              |

**Preferred — signed release artifacts.** Every component publishes a release
per `vX.Y.Z` tag with three assets: the tarball, a Sigstore **attestation
bundle** (`.sigstore.json`) proving GitHub Actions built it from that tag, and
`SHA256SUMS`. Verification uses the GitHub CLI (`gh`, from GitHub's apt
repository as in section 3.1) but needs no GitHub account or login: the bundle
is downloaded from the release and passed with `--bundle`. (The bare
`gh attestation verify` form shown in GitHub's documentation fetches the
attestation through the API and requires `gh auth login`.) The same four steps
apply to every component; the tags are the `*_VERSION` keys in `node.env`.
Verification requires provenance from the expected component repository and the
exact signing workflow at that release tag. The workflow is
`seismic-release.yml` for Reth and `release.yml` for the other components;
unknown repositories are rejected. A different tag or workflow is rejected even
within the same repository:

```bash
fetch() {  # fetch <repository> <tag> <binary>...
    local repo=$1 tag=$2; shift 2
    local workflow
    case "$repo" in
        seismic-reth) workflow=seismic-release.yml ;;
        summit|enclave|seismic-checkpointer) workflow=release.yml ;;
        *) printf 'No trusted release workflow for %s\n' "$repo" >&2; return 1 ;;
    esac
    local identity="https://github.com/SeismicSystems/$repo/.github/workflows/$workflow@refs/tags/$tag"
    local base="https://github.com/SeismicSystems/$repo/releases/download/$tag"
    local asset="${1}_${tag}_linux_amd64"
    curl -fsSLO "$base/$asset.tar.gz"
    curl -fsSLO "$base/$asset.sigstore.json"
    curl -fsSLO "$base/SHA256SUMS"
    sha256sum --ignore-missing -c SHA256SUMS
    tar -xzf "$asset.tar.gz"
    for bin in "$@"; do
        gh attestation verify "$bin" --repo "SeismicSystems/$repo" \
            --cert-identity "$identity" --bundle "$asset.sigstore.json" \
            || return 1
        sudo install -o root -g root -m 0755 "$bin" "/usr/local/bin/$bin"
    done
    rm -f SHA256SUMS
}
fetch summit               v0.1.0 summit
fetch seismic-reth         v0.1.0 seismic-reth mdbx_copy   # mdbx_copy only needed with the checkpointer
fetch enclave              v0.1.1 seismic-centralized-custodian-service
fetch seismic-checkpointer v0.1.1 seismic-checkpointer      # when enabled
```

The seismic-reth tarball carries `mdbx_copy`, built from the libmdbx revision
that tree vendors, so the two always match; the one bundle attests both
binaries.

**Alternative — build from source** on a build host or on the node, pinned to
the same tags. Building needs the packages listed under 3.1 and a Rust toolchain
for the building user:

```bash
git clone --branch v0.1.0 https://github.com/SeismicSystems/summit.git
(cd summit && cargo build --locked --release --features prom)
sudo install -o root -g root -m 0755 summit/target/release/summit /usr/local/bin/summit

git clone --branch v0.1.0 https://github.com/SeismicSystems/seismic-reth.git
# The release profile for Reth is maxperf (fat LTO, one codegen unit), as in
# seismic-reth's release workflow; it builds slower and lands in target/maxperf.
(cd seismic-reth && cargo build --locked --profile maxperf --bin seismic-reth)
sudo install -o root -g root -m 0755 seismic-reth/target/maxperf/seismic-reth /usr/local/bin/seismic-reth

git clone --branch v0.1.1 https://github.com/SeismicSystems/enclave.git
(cd enclave && cargo build --locked --release -p seismic-centralized-custodian-service)
sudo install -o root -g root -m 0755 enclave/target/release/seismic-centralized-custodian-service \
        /usr/local/bin/seismic-centralized-custodian-service
```

When the checkpointer is enabled, also build it and `mdbx_copy`. `mdbx_copy`
must come from the **same** seismic-reth checkout as the Reth binary so its
libmdbx matches Reth's database format. With `RETH_INSTALL_METHOD=prebuilt` the
installer cannot derive it from the Reth binary, so point
`RETH_MDBX_COPY_PREBUILT_PATH` at an `mdbx_copy` built as below from the same
checkout; the installer compares the libmdbx `git describe` string embedded in
both executables and refuses to install a mismatched pair:

```bash
git clone --branch v0.1.1 https://github.com/SeismicSystems/seismic-checkpointer.git
(cd seismic-checkpointer && cargo build --locked --release)
sudo install -o root -g root -m 0755 seismic-checkpointer/target/release/seismic-checkpointer /usr/local/bin/seismic-checkpointer

make -C seismic-reth/crates/storage/libmdbx-rs/mdbx-sys/libmdbx mdbx_copy
sudo install -o root -g root -m 0755 seismic-reth/crates/storage/libmdbx-rs/mdbx-sys/libmdbx/mdbx_copy /usr/local/bin/mdbx_copy
```

### 3.5 Checkpoint runner

_Installer step: `runner`._

Only needed when Summit will be started **from a checkpoint**. The
`seismic-summit-checkpoint` and `seismic-observer-checkpoint` units execute it
instead of `summit` directly; the normal-start units do not use it. A node that
only ever starts from genesis or from its own state can skip this step, but
since joining a running network by deposit (section 4) starts from a checkpoint,
validators normally need it.

Why a wrapper exists: the checkpoint and weak-subjectivity paths are written by
`seismic-node checkpoint install` into `/etc/seismic/checkpoint-start.toml`, not
by the operator into `node.env`, and systemd can only expand variables from
`EnvironmentFile=`. The runner reads that file and, on every start, before
exec'ing Summit with `--checkpoint-path` and `--weak-subjectivity-path`
appended:

- checks the checkpoint directory is structurally complete and the
  weak-subjectivity file is root-owned, so a half-copied checkpoint fails with a
  clear message rather than deep inside Summit;
- refuses `--unsafe-skip-checkpoint-verification` and the other checkpoint flags
  if anyone has edited them into the unit, so the unit cannot bypass
  verification;
- holds the Summit process lock across the exec, so a start cannot race a
  checkpoint installation.

It `exec`s Summit, so no wrapper process remains: systemd sees Summit's PID and
signals reach it directly.

The runner is part of this repository, not of any Summit release, so it is
installed the same way whether the binaries above were downloaded or built.

```bash
sudo install -o root -g root -m 0755 "$REPO/tools/checkpoint-start/summit-checkpoint-runner.py" \
        /usr/local/libexec/seismic/summit-checkpoint-runner
```

### 3.6 Genesis and `node.env`

_Installer step: `config`._

```bash
sudo install -o root -g root -m 0644 "$REPO/internal_testnet_genesis.toml" /etc/seismic/genesis.toml
sudo install -o root -g root -m 0640 "$REPO/install/templates/node.env.example" /etc/seismic/node.env
```

Edit `/etc/seismic/node.env`. Its comments (from `node.env.example`) document
every key; the ones you must decide are:

| Key                                                                                                                                      | Set to                                                                                         |
| ---------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `SEISMIC_ROLE`                                                                                                                           | `validator` or `observer`                                                                      |
| `RETH_BOOTNODE` / `RETH_BOOTNODES_ARG`                                                                                                   | The `enode` from `POST <bootnode>/rpc` method `seismic_nodeInfo`, or both empty                |
| `SUMMIT_BOOTSTRAPPERS_FILE` / `SUMMIT_BOOTSTRAPPERS_ARG`                                                                                 | `/persistence/keys/summit/bootstrappers.toml` and `--bootstrappers <that path>`, or both empty |
| `CUSTODIAN_COUNCIL_ADDRESS`, `CUSTODIAN_CHAIN_ID`                                                                                        | Address has a testnet default; chain ID from Seismic operations                                |
| `CHECKPOINTER_ENABLED`                                                                                                                   | Usually `false` for a plain validator                                                          |
| `TLS_MODE`, `DOMAIN`, `CUSTODIAN_PUBLIC_URL`                                                                                             | See [3.11](#311-https-endpoint); validators must expose the Custodian                          |
| `MONITORING_MODE`, `MONITORING_NODE_NAME`, `MONITORING_REMOTE_WRITE_URL`                                                                 | See [3.12](#312-monitoring); empty mode means push for validators, off for observers           |
| **observer** `OBSERVER_PARENT_NODE_PUBLIC_KEY`, `OBSERVER_INDEX`, `OBSERVER_PUBLIC_ADDRESS`, `OBSERVER_P2P_PORT`, `CUSTODIAN_PARENT_URL` | From the parent's operator                                                                     |

Keys marked `(computed)` in `node.env.example` (`RETH_BOOTNODES_ARG`,
`SUMMIT_BOOTSTRAPPERS_ARG`, `OBSERVER_P2P_PORT`) repeat another key in the form
a unit needs, because systemd cannot compute one value from another; the
validator checks they agree with their source. Keys marked `(default)` are
conventional paths that normally stay as they are.

Validate before going further. This applies exactly the parsing rules systemd
uses, so what passes here is what the units will see:

```bash
(cd "$REPO/tools" && sudo python3 -m seismic_node.nodeenv --check /etc/seismic/node.env)
```

### 3.7 Keys

_Installer step: `keys`._

Reth P2P identity:

```bash
openssl rand -hex 32 | tr -d '\n' | sudo tee /persistence/keys/reth/p2p-key >/dev/null
sudo chown seismic:seismic /persistence/keys/reth/p2p-key
sudo chmod 0600 /persistence/keys/reth/p2p-key
```

**validator** — Summit node and consensus keys:

```bash
sudo -u seismic /usr/local/bin/summit keys generate --key-store-path /persistence/keys/summit -y
sudo chmod 0600 /persistence/keys/summit/*.pem
```

**observer** — an observer uses the **parent validator's** `node_key.pem`
(provisioned by the parent's operator) and its own consensus key. Generate the
consensus key in a scratch key store, keep only that file, install the
provisioned node key, and record the assignment the CLI verifies on every start:

```bash
tmp=$(sudo -u seismic mktemp -d /persistence/keys/.gen.XXXXXX)
sudo -u seismic /usr/local/bin/summit keys generate --key-store-path "$tmp" -y
sudo install -o seismic -g seismic -m 0600 "$tmp/consensus_key.pem" /persistence/keys/summit/consensus_key.pem
sudo rm -rf "$tmp"
sudo install -o seismic -g seismic -m 0600 /path/to/provisioned/node_key.pem /persistence/keys/summit/node_key.pem
printf '%s:%s\n' "$OBSERVER_PARENT_NODE_PUBLIC_KEY" "$OBSERVER_INDEX" | sudo tee /persistence/keys/summit/observer-assignment >/dev/null
sudo chown seismic:seismic /persistence/keys/summit/observer-assignment
sudo chmod 0600 /persistence/keys/summit/observer-assignment
```

`summit keys show --key-store-path /persistence/keys/summit` must print
`OBSERVER_PARENT_NODE_PUBLIC_KEY`.

Optional bootstrappers file, when joining a network whose genesis peers are
stale:

```bash
sudo install -o seismic -g seismic -m 0640 bootstrappers.toml /persistence/keys/summit/bootstrappers.toml
```

Back up `/persistence/keys` securely. Never paste key material into tickets,
chat, or source control.

### 3.8 Checkpointer configuration (optional)

_Installer step: `checkpointer`._

When `CHECKPOINTER_ENABLED=true`, render
`install/templates/config/seismic-checkpointer.toml` to
`/etc/seismic/seismic-checkpointer.toml`, substituting these variables (the
binary paths are fixed in the template):

| Placeholder                  | Value                                      |
| ---------------------------- | ------------------------------------------ |
| `${RETH_DB_PATH}`            | `$RETH_DATA_DIR/db`                        |
| `${CHECKPOINTS_DIR}`         | `$CHECKPOINTS_DIR`                         |
| `${CHECKPOINTER_STATE_FILE}` | `$CHECKPOINTS_DIR/checkpointer-state.cbor` |

### 3.9 Custodian root key

_No installer step; the Custodian creates the key itself._

Nothing to install. The Custodian keeps its 32-byte root key at
`$CUSTODIAN_DATA_DIR/root.key` and creates it on first start:

- **validator** — if the file is absent, the Custodian writes the **publicly
  known** epoch-0 default itself, so every node agrees on epoch-0 keys with no
  coordination. It logs a warning on every start while that key is in use;
  epoch-0 keys provide no confidentiality until the council delivers epoch 1. To
  use a secret root key instead, pre-place 32 random bytes (`openssl rand 32`,
  mode `0600`, owner `seismic`) before the first start.
- **observer** — the file must be absent on first start; the observer Custodian
  fetches and verifies the root key through `CUSTODIAN_PARENT_URL` and never
  pins the default. The parent's HTTPS endpoint must be reachable with a trusted
  certificate before that.

### 3.10 systemd units

_Installer step: `units`._

```bash
for unit in "$REPO"/install/templates/systemd/seismic-*.service; do
    name=$(basename "$unit")
    sudo install -o root -g root -m 0644 "$unit" "/etc/systemd/system/$name"
    sudo install -d -o root -g root -m 0755 "/etc/systemd/system/$name.d"
    sudo install -o root -g root -m 0644 "$REPO/install/templates/systemd/seismic-hardening.conf" \
            "/etc/systemd/system/$name.d/10-hardening.conf"
done
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/seismic-*.service
```

Installing all ten units on every host is intentional: the units that do not
apply to this role or to disabled features are inert — nothing starts them, and
`seismic-node` only touches the ones `node.env` enables.

### 3.11 HTTPS endpoint

_Installer step: `tls`._

`TLS_MODE` selects one of:

- `disabled` — no public endpoint. **Observers only**: their Custodian needs
  just outbound access to the parent, whereas a validator's serves purpose keys
  to its observers and must be reachable, so `--check` rejects this mode for
  validators.
- `external` — you run the terminator. Set `CUSTODIAN_PUBLIC_URL` to the base
  URL you expose (e.g. `https://node.example.com/custodian`) and give your proxy
  the Custodian route. Reference configurations that implement every rule in
  [`install/CUSTODIAN_TLS.md`](../install/CUSTODIAN_TLS.md#proxy-requirements)
  are in `install/templates/proxy/`: `nginx/custodian.conf` (a server block for
  plain nginx), `caddy/Caddyfile`, and `envoy/envoy.yaml` (a static bootstrap).
  Replace `DOMAIN` and, if changed, `CUSTODIAN_COUNCIL_PORT`; each file states
  where it falls short of the managed OpenResty policy (Caddy needs a plugin for
  rate limiting, Envoy an external service for per-IP limits). The rules in one
  line: forward `POST /custodian/v1/council` to
  `http://127.0.0.1:$CUSTODIAN_COUNCIL_PORT/v1/council` with client headers
  stripped, cap bodies at 64 KiB, never log or cache bodies, never serve the
  route over plain HTTP.
- `custodian-only` / `full` — managed OpenResty with automatic certificates from
  Let's Encrypt via `lua-resty-auto-ssl` (`DOMAIN` must resolve to this host;
  ports 80 and 443 open). `custodian-only` exposes only
  `POST /custodian/v1/council`. `full` additionally proxies the node routes
  below and needs the JWT secret at `OPENRESTY_JWT_SECRET_PATH`, which the
  installer generates.

  | Public path     | Local upstream    | Notes                                            |
  | --------------- | ----------------- | ------------------------------------------------ |
  | `/rpc`          | `127.0.0.1:8545`  | Reth HTTP JSON-RPC                               |
  | `/ws`           | `127.0.0.1:8546`  | Reth WebSocket JSON-RPC                          |
  | `/summit`       | `127.0.0.1:3030`  | Summit RPC                                       |
  | `/ops`          | `127.0.0.1:8552`  | Signature-authenticated privileged Reth RPC      |
  | `/checkpointer` | `127.0.0.1:42069` | Rate-limited and JWT-protected RPC and snapshots |

  With `MONITORING_MODE=pull` ([3.12](#312-monitoring)) both managed modes also
  render one JWT-protected, rate-limited, GET-only route per metrics endpoint —
  in `custodian-only` these and `/custodian` are the only public paths, so
  publishing metrics never requires publishing the node's RPC:

  | Public path          | Local upstream                              |
  | -------------------- | ------------------------------------------- |
  | `/prom-summit`       | `127.0.0.1:9090/`                           |
  | `/prom-reth`         | `127.0.0.1:9001/`                           |
  | `/prom-custodian`    | `127.0.0.1:$CUSTODIAN_COUNCIL_PORT/metrics` |
  | `/prom-checkpointer` | `127.0.0.1:42069/metrics` (if enabled)      |

  In every other monitoring mode these routes do not exist and
  `/checkpointer/metrics` returns 404, so no metrics leave the node through the
  proxy.

  JWT-protected routes require `Authorization: Bearer <token>` signed with the
  secret at `OPENRESTY_JWT_SECRET_PATH` (`tools/generate-openresty-jwt.sh`
  issues one); the installer generates the secret whenever such routes exist.
  Installing the configuration never starts, enables, or reloads OpenResty;
  activate it explicitly once the node runs:

  ```bash
  sudo openresty -t
  sudo systemctl enable openresty
  sudo systemctl is-active --quiet openresty && sudo systemctl reload openresty || sudo systemctl start openresty
  ```

  Reth, Summit, checkpointer, and metrics listeners stay bound to loopback in
  every mode; OpenResty is the only public surface, and `seismic-deposit-rpc`
  (`3031`) is never proxied. `node.env` has no key that moves these listeners:
  the checkpointer and the managed proxy reach them on loopback, so a terminator
  on another host needs a tunnel (SSH, WireGuard) to this one rather than a
  wider bind. Proxy requirements for the Custodian route are in
  [`install/CUSTODIAN_TLS.md`](../install/CUSTODIAN_TLS.md).

Validators must expose Custodian; give Seismic operations the resulting
`https://DOMAIN/custodian` URL.

### 3.12 Monitoring

_Not an installer step; in push mode run `seismic-node monitoring install` after
installation._

Every service serves Prometheus metrics on loopback, in every mode:

| Service                                    | Loopback endpoint                           | Push job / pull route                        |
| ------------------------------------------ | ------------------------------------------- | -------------------------------------------- |
| Summit                                     | `127.0.0.1:9090/`                           | `summit/<node>` / `/prom-summit`             |
| Reth                                       | `127.0.0.1:9001/`                           | `reth/<node>` / `/prom-reth`                 |
| Custodian                                  | `127.0.0.1:$CUSTODIAN_COUNCIL_PORT/metrics` | `custodian/<node>` / `/prom-custodian`       |
| Checkpointer (`CHECKPOINTER_ENABLED=true`) | `127.0.0.1:42069/metrics`                   | `checkpointer/<node>` / `/prom-checkpointer` |
| Prometheus Agent (push only)               | `127.0.0.1:9091`                            | `prometheus-agent/<node>` / —                |

Seismic operations monitor **validators only**: observers never push to Seismic
and are never scraped by it. Observers are typically run by RPC providers with
their own metrics pipeline, which can scrape the loopback endpoints directly;
the agent is available to them as well, pointed at their own Prometheus.
`MONITORING_MODE` decides how metrics leave the node. Leave it empty for the
role's default, `push` for validators and `off` for observers:

- `push` (validator default) — the **Prometheus Agent** scrapes the endpoints
  locally and remote-writes them. A validator pushes to Seismic's central
  Prometheus with a bearer token from Seismic operations, and optionally also to
  a Prometheus of its own; an observer pushes to the operator's own Prometheus
  only (`MONITORING_OPERATOR_REMOTE_WRITE_URL` is required). Nothing inbound is
  needed. **The metrics endpoints must not be exposed publicly in this mode**:
  the managed OpenResty renders no `/prom-*` routes, and if you run your own
  proxy (`TLS_MODE=external`) do not add any — Seismic would collect every
  series twice, and the endpoints carry no authentication of their own.
- `pull` (validators only) — the HTTPS proxy publishes the endpoints as
  JWT-protected `/prom-*` routes ([3.11](#311-https-endpoint)) and Seismic's
  central Prometheus scrapes them. Requires an HTTPS proxy on the node:
  `TLS_MODE=custodian-only` or `full`, where the managed OpenResty renders the
  routes from `install/templates/openresty/metrics-location.conf` (the node's
  RPC does not have to be public for this), or `TLS_MODE=external`. **With your
  own proxy you must add the same protection yourself**: one GET-only route per
  endpoint, proxying exactly the upstream path in the 3.11 table, rate-limited,
  and rejecting any request whose `Authorization: Bearer <token>` is not a JWT
  signed with the secret at `OPENRESTY_JWT_SECRET_PATH` (the check
  `install/templates/openresty/lua/jwt_auth.lua` implements; strip the header
  before proxying). Mint a token with `tools/generate-openresty-jwt.sh` and hand
  it to Seismic operations together with the public base URL. Never publish the
  metrics ports without that check.
- `off` (observer default) — nothing leaves the node. For a validator only by
  agreement with Seismic operations, who otherwise alert on the missing
  telemetry.

Which combinations with `TLS_MODE` ([3.11](#311-https-endpoint)) are valid, and
what is public on the node in each (`no` means rejected by `--check`; observers
can only use `off` or `push`):

| `TLS_MODE`                  | `push` (default)                          | `pull`                                          | `off`                     |
| --------------------------- | ----------------------------------------- | ----------------------------------------------- | ------------------------- |
| `disabled` (observers only) | to your own Prometheus only               | no: Seismic does not scrape observers           | nothing public            |
| `custodian-only`            | `/custodian`                              | `/custodian`, `/prom-*`                         | `/custodian`              |
| `full`                      | `/custodian`, node routes                 | `/custodian`, node routes, `/prom-*`            | `/custodian`, node routes |
| `external`                  | your routes; must **not** include metrics | your routes; must include JWT-checked `/prom-*` | your routes               |

The rest of this section is push mode. Its CLI is `seismic-node monitoring` and
its settings in `node.env` are:

| Key                                    | Set to                                                                                                                                             |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MONITORING_NODE_NAME`                 | The `node` label on every series. For a validator, exactly the name Seismic operations registered the node and its token under                     |
| `MONITORING_REMOTE_WRITE_URL`          | **validator**: the HTTPS remote-write endpoint from Seismic operations                                                                             |
| `MONITORING_DATA_DIR`                  | The agent's WAL directory, owned by its own `seismic-prometheus` account and outside node state (default `/var/lib/seismic-prometheus-agent`)      |
| `MONITORING_OPERATOR_REMOTE_WRITE_URL` | A remote-write endpoint you run yourself; optional for validators, required for observers. See [Seeing your own metrics](#seeing-your-own-metrics) |

**Validators.** The token is the bearer credential the agent presents on every
remote-write request to Seismic; Seismic operations issues one per node (64 hex
characters) and registers it against `MONITORING_NODE_NAME`. Put it in a
root-only file outside the node's data and key directories — the CLI refuses
anything else — and run:

```bash
sudo install -o root -g root -m 0600 /path/from/ops/token /root/monitoring-token
sudo "$REPO/tools/seismic-node.py" monitoring install --token-source /root/monitoring-token
```

This creates the `seismic-prometheus` account, downloads the checksum-pinned
Prometheus release, renders `/etc/seismic/prometheus-agent/prometheus.yml` and
`targets.json` (the endpoint list above, derived from `node.env`), and copies
the token. It starts nothing.

The agent scrapes every endpoint in `targets.json` and itself (`9091`) every 15
seconds, labelling each series with `node=<MONITORING_NODE_NAME>`,
`instance=<MONITORING_NODE_NAME>`, `role=<SEISMIC_ROLE>`, `service=<service>`
and `job=<service>/<node>`, and remote-writes over outbound HTTPS; port `9091`
is a local status listener, not an inbound requirement. Its WAL keeps at least 5
minutes and at most 6 hours of unsent samples — an outage longer than that loses
data, so watch free space. Seismic operations must register the node as a push
source before the agent starts, and stop any central pull of the same node so
series are not collected twice.

#### Seeing your own metrics

The agent keeps nothing locally: it has no storage and no query API, so in push
mode there is nothing on the node for a Grafana to read. To see your node's
metrics yourself, have the agent deliver the samples to a Prometheus you run.
For an observer this is the agent's only destination, and step 4 runs without
`--token-source`:

1. Run a Prometheus (not on the validator) with
   `--web.enable-remote-write-receiver`, behind HTTPS, and point your Grafana at
   it.
2. Set
   `MONITORING_OPERATOR_REMOTE_WRITE_URL=https://<your host>[:port]/api/v1/write`
   in `node.env`.
3. If your receiver requires a bearer token, put it in a root-only file and pass
   `--operator-token-source <file>` to `monitoring install`; omit the flag for a
   receiver that authenticates another way.
4. Stop the agent (`monitoring install` refuses to replace a running agent's
   files), rerun the install, and start it again:

   ```bash
   sudo "$REPO/tools/seismic-node.py" monitoring stop
   sudo "$REPO/tools/seismic-node.py" monitoring install --token-source /root/monitoring-token \
       [--operator-token-source /root/operator-token]
   sudo "$REPO/tools/seismic-node.py" monitoring start
   ```

The agent then forwards every sample to both destinations from separate queues:
if your receiver is down, delivery to Seismic continues, and the other way
round. Your Prometheus gets the same series and labels Seismic gets, so a
dashboard built against either works against both. [`METRICS.md`](METRICS.md)
describes what each service exports and what a healthy node looks like;
[`monitoring/`](../monitoring/README.md) holds alert rules and a Grafana
dashboard for it, ready to load. To stop, clear the key, rerun
`monitoring install` without `--operator-token-source`, and restart the agent;
the second destination and its token are removed.

### 3.13 Pre-start verification

```bash
(cd "$REPO/tools" && sudo python3 -m seismic_node.nodeenv --check)
sudo systemctl list-units --all 'seismic-*'          # all inactive
sudo ls -l /persistence/keys/reth/p2p-key /persistence/keys/summit/   # seismic:seismic, 0600
/usr/local/bin/seismic-reth --version
/usr/local/bin/summit --version
sudo test ! -e /etc/seismic/checkpoint-start.toml     # true on a fresh host
```

## 4. First start

How a node starts the first time depends on whether its keys are in the genesis
file. Use the `seismic-node` CLI for all of these; it validates `node.env`,
checks the installed keys match, and starts units in dependency order (Custodian
→ Reth → Summit → checkpointer), stopping what it started if a later step fails.

### Validator listed in genesis

```bash
sudo "$REPO/tools/seismic-node.py" validator start --mode normal
```

### Validator joining a running network

1. Produce the deposit-signature file and send it to Seismic operations:

   ```bash
   sudo "$REPO/tools/seismic-node.py" validator deposit-signature --output /root/deposit-signature.json
   ```

   This starts `seismic-deposit-rpc.service` for a few seconds and stops it.

2. After operations confirms the deposit, onboard from a checkpoint. The
   weak-subjectivity anchor **must** come from a source independent of the
   snapshot provider:

   ```bash
   sudo "$REPO/tools/seismic-node.py" validator onboard \
     --summit-rpc-url https://trusted-validator.example/summit \
     --snapshot-api-url https://snapshot.example/checkpointer \
     --snapshot-bearer-token-file /root/snapshot-token \
     --weak-subjectivity-rpc-url https://independent-validator.example/summit
   ```

   The command waits for the account to reach `Joining`, installs the checkpoint
   (writing `/etc/seismic/checkpoint-start.toml` and `weak-subjectivity.toml`,
   with a rollback backup under `/persistence/checkpoint-rollback/`), and starts
   `seismic-summit-checkpoint.service`.

### Observer

```bash
sudo "$REPO/tools/seismic-node.py" observer start --mode normal
```

or, to start from a checkpoint, run `checkpoint install` first (see
[Checkpoints](#checkpoints) in section 5) and then
`observer start --mode checkpoint`.

### Verify

```bash
systemctl status 'seismic-*'
journalctl -u seismic-reth -u seismic-summit -f
curl -s 127.0.0.1:9001/metrics | grep -E '^reth_sync_'        # Reth height
curl -s 127.0.0.1:9090/ | grep -E 'peers|height' | head        # Summit
curl -s -X POST 127.0.0.1:8545 -H 'content-type: application/json' \
     -d '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}'
```

The units use `Type=simple`: a successful start and an immediate `active` state
do not guarantee successful `exec` or application readiness. Check the journals
and health endpoints; execution or initialization can still fail after the CLI
returns.

Expect Reth to report peers within a minute and Summit to log participation once
synced. Every host must use an identical genesis; a digest mismatch shows up as
P2P authentication failures in the Summit journal.

### Enabling at boot

Units are installed disabled. A reboot is not a crash: it usually follows
maintenance such as a new binary, a restored checkpoint, or a changed
`node.env`, and `seismic-node … start` runs identity and checkpoint checks that
a bare `systemctl start` does not. Whether to enable units at boot is an
operational choice, not a safety one:

- **validator** — the shipped policy is to leave `seismic-reth` and the Summit
  units disabled and run `seismic-node validator start` after a reboot. The
  auxiliary units may be enabled. Every unit is installed on every host; the two
  optional ones check `node.env` before starting (`ExecCondition=`) and skip
  cleanly unless `CHECKPOINTER_ENABLED=true` or, for the agent, the effective
  `MONITORING_MODE` is `push`, so they can stay enabled across a later change of
  mode. The agent still needs `seismic-node monitoring install` before its first
  start.

  ```bash
  sudo systemctl enable seismic-custodian seismic-checkpointer seismic-prometheus-agent
  ```

- **observer** — enabling the observer Summit unit also pulls in Reth at boot:

  ```bash
  sudo systemctl enable seismic-observer seismic-observer-custodian seismic-checkpointer
  ```

Operators who prefer validators to come back unattended after a reboot may
enable `seismic-summit` (or `seismic-summit-checkpoint`) as well; the
double-sign protection described under [Restart policy](#restart-policy) applies
equally to a boot-time start.

## 5. Operations

### Status and logs

```bash
systemctl status 'seismic-*'
journalctl -u seismic-summit --since -1h
journalctl -u seismic-reth -p warning
systemd-analyze security seismic-reth.service
```

### Stop and start

```bash
sudo "$REPO/tools/seismic-node.py" validator stop        # or: observer stop
sudo "$REPO/tools/seismic-node.py" validator start --mode normal|checkpoint
```

`stop` stops units in reverse order and leaves OpenResty and the Prometheus
Agent running. Equivalent manual commands are printed by `--help`; the units
declare `Conflicts=` so the normal and checkpoint Summit variants can never run
together.

`systemctl restart seismic-reth` restarts Summit with it (`Requires=` propagates
the restart job; Summit does not fail or wait `RestartSec`), ordered after Reth
is back. For a controlled restart prefer `validator stop` followed by
`validator start`, which also re-runs the CLI's checks.

### Upgrading a binary

1. `seismic-node validator stop` (or `observer stop`).
2. Install the new binary at its fixed path (step 3.4). Update `*_VERSION` in
   `node.env` so the file records what is deployed.
3. `seismic-node validator start`.

Nothing else changes: units do not reference versions. Upgrade the components
together, to the set a release of this repository pins in `node.env.example`,
unless its notes say one may move alone.

### Checkpoints

Install a checkpoint on a stopped node:

```bash
sudo "$REPO/tools/seismic-node.py" checkpoint install --role validator \
  --snapshot-api-url https://snapshot.example/checkpointer \
  --snapshot-bearer-token-file /root/snapshot-token \
  --weak-subjectivity-rpc-url https://independent-validator.example/summit
```

Roll back to the state before the last install:

```bash
sudo "$REPO/tools/seismic-node.py" checkpoint rollback --role validator
```

Backups are never deleted automatically; `checkpoint delete-backup` removes one
after explicit confirmation.

### Prometheus Agent

```bash
sudo "$REPO/tools/seismic-node.py" monitoring status|start|stop|check
```

`check` confirms the installed agent still matches `node.env`.

## 6. Changing the configuration

1. Edit `/etc/seismic/node.env`.
2. Validate:
   `(cd "$REPO/tools" && sudo python3 -m seismic_node.nodeenv --check)`.
3. If a rendered file depends on the changed keys, re-render it:
   `seismic-checkpointer.toml` (step 3.8), OpenResty `nginx.conf` (3.11),
   `prometheus.yml` (`monitoring install`).
4. Restart the affected units:
   `sudo "$REPO/tools/seismic-node.py" validator stop` then `… start`, or
   `sudo "$REPO/tools/seismic-node.py" monitoring stop` then `… install` and
   `… start` for the agent (it re-reads `node.env` only at install time).

systemd reads `node.env` at each unit start, so a change takes effect on the
next start of that unit and not before.

### Switching `MONITORING_MODE`

This is the one change that touches both the proxy and the agent and needs a
handover with Seismic operations, who collect each node either by push or by
pull, never both, or every series arrives twice. Agree the switch with them
first, then:

**`push` → `pull`**

```bash
sudo "$REPO/tools/seismic-node.py" monitoring stop
# edit /etc/seismic/node.env: MONITORING_MODE=pull
(cd "$REPO/tools" && sudo python3 -m seismic_node.nodeenv --check)
sudo "$REPO/install/install.sh" --config /etc/seismic/node.env --only tls
sudo openresty -t && sudo systemctl reload openresty
sudo "$REPO/tools/generate-openresty-jwt.sh" --ttl-seconds 15552000 --subject seismic-ops
```

The `tls` step renders the `/prom-*` routes and, on a `custodian-only` host that
had none before, creates the JWT secret at `OPENRESTY_JWT_SECRET_PATH`. Hand the
printed token and `https://DOMAIN` to Seismic operations; they register the pull
target and retire the push one. The agent's token and WAL stay on disk unused;
if the unit is enabled at boot it stays enabled but its `ExecCondition=` skips
the start while `MONITORING_MODE` is not `push`. With `TLS_MODE=external`, add
the JWT-checked routes to your own proxy instead of running the `tls` step
([3.12](#312-monitoring)).

**`pull` → `push`**

```bash
# edit /etc/seismic/node.env: MONITORING_MODE=push and the MONITORING_* keys
(cd "$REPO/tools" && sudo python3 -m seismic_node.nodeenv --check)
sudo "$REPO/tools/seismic-node.py" monitoring install --token-source /root/monitoring-token
sudo "$REPO/install/install.sh" --config /etc/seismic/node.env --only tls
sudo openresty -t && sudo systemctl reload openresty
sudo "$REPO/tools/seismic-node.py" monitoring start
```

The `tls` step removes the `/prom-*` routes (the JWT secret remains for any
other protected routes). Tell Seismic operations once the agent is running so
they retire the pull target. With `TLS_MODE=external`, remove the metrics routes
from your own proxy instead.

## 7. Running as a different account

The service account is fixed to `seismic` in the unit files and in the Custodian
socket ACL. To use another account, override the three places it appears and
re-own the state. Everything below writes to `/etc` and `/persistence`, so run
it in a root shell (`sudo -i`):

```bash
NEW=ops
useradd --system --home /var/lib/seismic --shell /usr/sbin/nologin --user-group "$NEW"
for u in /etc/systemd/system/seismic-*.service; do
    [ "$(basename "$u")" = seismic-prometheus-agent.service ] && continue
    printf '[Service]\nUser=%s\nGroup=%s\n' "$NEW" "$NEW" > "$u.d/20-user.conf"
done
# Custodian grants socket access by uid; it must name the account Reth runs as.
# Append to the drop-in written above so User=/Group= survive, and cover whichever
# Custodian unit this host has (validator or observer).
for u in /etc/systemd/system/seismic-custodian.service /etc/systemd/system/seismic-observer-custodian.service; do
    [ -f "$u" ] || continue
    printf 'ExecStart=\n' >> "$u.d/20-user.conf"
    sed -n '/^ExecStart=/,/--allow/p' "$u" \
        | sed "s/--allow seismic:/--allow $NEW:/" >> "$u.d/20-user.conf"
done
chown -R "$NEW:$NEW" /persistence/reth /persistence/summit /persistence/keys \
                     /persistence/custodian /persistence/checkpoints 2>/dev/null
systemctl daemon-reload
```

Drop-ins survive reinstalling the unit files; editing the units in place does
not.

## 8. Automating with configuration management

Every step above is a file copy, a package install, or an idempotent command, so
it maps directly onto a configuration-management role:

| Step                     | Ansible equivalent                                                                                                          |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| 3.1 packages             | `apt`                                                                                                                       |
| 3.2 account, 3.3 dirs    | `user`, `file`                                                                                                              |
| 3.4 binaries, 3.5 runner | `get_url` + `stat` checksum, or `copy` from a build host                                                                    |
| 3.6 `node.env`           | `template` from `node.env.example`, then `command: python3 -m seismic_node.nodeenv --check`                                 |
| 3.7 keys                 | `command` with `creates:`; never templated                                                                                  |
| 3.8, 3.11 rendered files | `template`                                                                                                                  |
| 3.10 units               | `copy` ×10 units + the hardening drop-in, `systemd: daemon_reload`                                                          |
| 3.12 Prometheus Agent    | `command: seismic-node monitoring install`                                                                                  |
| 4 start                  | `command: seismic-node validator start` — or `systemd: state=started` per unit if you accept the CLI's checks being skipped |

The installer (`install.sh --config node.env`) performs steps 3.1–3.11 from a
prepared `node.env` for operators who prefer one command over a role:

```bash
sudo "$REPO/install/install.sh" --config /path/to/node.env            # everything
sudo "$REPO/install/install.sh" --config /path/to/node.env --dry-run  # validate and print the plan
sudo "$REPO/install/install.sh" --config /path/to/node.env --only units,tls
```

Every step is idempotent and reports what it changed; a rerun after editing
`node.env` or a template touches only what differs. Nothing is started.
