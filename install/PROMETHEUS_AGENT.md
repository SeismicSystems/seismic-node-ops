# Optional Prometheus Agent

Both node installers can install an optional, independent Prometheus Agent.
Fresh installations default to disabled. Installing the component starts no
services. Silencing and maintenance exclusions are not part of this feature.

## Prerequisites

- Enable authenticated remote-write ingress on the monitoring VM and provision
  its DNS, TLS certificate, and TCP 443 access.
- Issue a token there with
  `sudo monitoring-token add <node> --output /root/<node>.token`. The helper
  lives in the separate deploy repository under
  `monitoring/monitoring-token.py`; the node installer does not issue tokens.
- Transfer the handoff securely to the node, owned by root with mode `0600`.
  Never pass the token itself on the command line. The installer asks for the
  **file path**, not its contents.
- Choose a stable lowercase hostname-style node label. Use the same name in the
  monitoring server's expected push inventory.

## Add monitoring to an existing node

From the `seismic-node-ops` checkout on the node:

```bash
# Validator: install/configure only Prometheus Agent.
sudo ./install/install-validator.sh --monitoring-only

# Observer: same workflow with observer role and inventory.
sudo ./install/install-observer.sh --monitoring-only

# Optional non-default installation inventory:
sudo ./install/install-validator.sh --monitoring-only \
  --inventory /etc/seismic/custom-validator.toml
```

This mode requires an existing root-managed installation inventory and a working
Supervisor control interface. It uses the same Ubuntu 24.04 / Python 3.12
preflight as the full installer, but **does not install system packages or run
any node installation steps**. Node services can remain running.

It prompts only for agent settings, validates the recorded node role and paths,
then shows a confirmation before applying changes. It preserves node binaries,
keys, data, OpenResty, and node Supervisor configuration. Only the agent's
resources and the inventory's `[monitoring]` table are updated; existing node
fields, comments, and inventory permissions are preserved. Node key contents are
not read. An old inventory without `[monitoring]` is supported. Missing,
untrusted, wrong-role, or unsupported inventories are refused rather than
reconstructed. Existing monitoring metadata must use the installer's standard
`[monitoring]` table, not inline/dotted alternatives.

An existing agent offers **keep** (default), **update**, and **disable**. Keep
leaves the agent untouched and can reconcile its saved settings into an outdated
inventory. Stop only the agent before selecting update or rotating its token:

```bash
sudo ./tools/seismic-node.py monitoring stop --role validator
sudo ./install/install-validator.sh --monitoring-only
```

Use the observer command/role for observers. Disable explicitly stops only the
agent, preserves credentials and WAL, and records it as disabled. Installation
and updates do not start the agent; when the metrics receiver is ready:

```bash
sudo ./tools/seismic-node.py monitoring start --role validator
sudo ./tools/seismic-node.py monitoring status --role validator
```

Successful later node startup also starts an enabled agent. Register the same
identity centrally as `PUSH_NODE`; do not collect it with both pull and push.

Monitoring-only runs hold a shared lock under `/run/seismic-prometheus-agent/`
and refuse an inventory changed during the operation. Do not run full
installers, node starts, or low-level agent helpers concurrently. Agent
configuration and node inventory are separate files: if agent installation
succeeds but the final atomic inventory update fails, inspect the agent and
rerun this mode with **keep** to reconcile the inventory. A failed agent
installation never publishes new monitoring metadata. No automatic rollback or
node restart is performed.

Without `--monitoring-only`, the installers still run the full node workflow.

## Installer configuration

Run `install/install-validator.sh` or `install/install-observer.sh` normally and
opt in at the Prometheus Agent prompt. The configuration review includes an
agent edit action.

Inputs:

- Remote-write URL: `https://metrics.example.com/api/v1/write`. Port 443, valid
  certificate verification, no URL credentials, query strings, or fragments.
- Stable node name: for example `internal-0.seismictest.net`.
- Root-only token handoff file.
- Dedicated WAL directory, default `/var/lib/seismic-prometheus-agent`. It must
  not overlap node data, keys, checkpoint directories, or managed configuration.

The installer downloads official Prometheus **3.5.0** for Linux amd64 or arm64,
checks a pinned SHA-256 checksum, and installs Prometheus and promtool under:

```text
/usr/local/lib/seismic/prometheus-agent/3.5.0/
```

Other paths:

```text
/etc/seismic/prometheus-agent/prometheus.yml
/etc/seismic/prometheus-agent/token
/etc/seismic/prometheus-agent/installation.json
/etc/supervisor/conf.d/prometheus-agent.conf
/var/log/seismic-prometheus-agent/
```

The dedicated `seismic-prometheus` account runs the agent. Configuration and
credentials are root-owned and group-readable only by that account. The settings
JSON is root-only and contains no credential. Non-secret settings are also
recorded in an optional `[monitoring]` table in the node installation inventory.
Older inventories without this table remain supported.

## Collection and labels

The agent scrapes every 15 seconds:

| Target | Local address    | Job                       |
| ------ | ---------------- | ------------------------- |
| Summit | `127.0.0.1:9090` | `summit/<node>`           |
| Reth   | `127.0.0.1:9001` | `reth/<node>`             |
| Agent  | `127.0.0.1:9091` | `prometheus-agent/<node>` |

Summit and Reth use path `/`, matching the existing OpenResty proxy upstreams;
the agent's own metrics use `/metrics`.

Each job has `node=<node>`, `instance=<node>`, and `role=validator` or
`role=observer`. No local OpenResty JWT is needed. The agent initiates outbound
HTTPS to the remote-write URL; its port 9091 is a **local** status/metrics
listener, not the destination. No new inbound firewall rule is required.

The bearer token authorizes ingestion; it does not enforce ownership of metric
labels. These credentials are for trusted node operators, not tenant isolation.

## Lifecycle

After successful validator or observer startup, `seismic-node` ensures a
configured agent is running. This covers normal and checkpoint start commands,
and the actual startup phase of validator onboarding. Waiting, refused
onboarding, identity failures, and checkpoint installation alone do not start
it. An already-running agent is left alone. A startup/verification failure emits
a warning without rolling back node startup or requiring remote connectivity.

The agent uses `autostart=false` and `autorestart=true`. It does not start
merely because the installer loads configuration. Once started, Supervisor
restarts it on unexpected exits, subject to Supervisor's startup retry limits.

**Node stop and startup rollback do not stop the agent.** It can continue
sending failed scrape results and draining buffered samples. Explicit commands:

```bash
sudo ./tools/seismic-node.py monitoring status --role validator
sudo ./tools/seismic-node.py monitoring start --role validator
sudo ./tools/seismic-node.py monitoring stop --role validator
```

Use `--role observer` for observers and `--inventory /absolute/path.toml` for a
custom inventory. Monitoring start updates only the agent's Supervisor group;
status and stop do not reload configuration or change node programs. A later
node start will ensure the agent runs again if it remains enabled in inventory.

## Reinstallation, rotation, and disabling

An existing installation offers **keep**, **update**, or **disable**. Keep is
the default and leaves the binary, configuration, token, WAL, and running
process untouched. The dedicated agent settings are reused, not the old node
inventory.

Before updating or rotating the token, explicitly stop the agent. Update refuses
running or indeterminate Supervisor states. If Supervisor is unavailable,
inspect and restore its control interface first; the installer will not assume
an agent is stopped. Choose update, provide the new handoff path (or retain the
installed token), and start the agent afterwards. A nonempty unrelated WAL
directory is never adopted. Generated configuration is checked with
`promtool check config --agent` before replacing credentials/configuration.

Disable explicitly stops the agent and removes its managed Supervisor file,
preserving the token and WAL. Successful installation records monitoring as
disabled. Disabling locally or deleting a handoff does **not** revoke the token;
revoke it separately on the monitoring VM when appropriate.

Do not run node installers, node starts, or agent updates concurrently.

## Buffering and monitoring-side rollout

The WAL is compressed. Minimum retention is 5 minutes; samples older than 6
hours may be forcibly deleted during truncation, even if they were never sent.
This is **not a disk-size limit or a six-hour delivery guarantee**. Monitor free
space, especially during outages. Remote-write concurrency is limited to two
shards and HTTP 429 responses are retried with backoff.

On the monitoring VM, replace that node's pull `NODE` entry with:

```text
PUSH_NODE internal-0.seismictest.net validator
PUSH_NODE observer-0.seismictest.net observer
```

The role defaults to validator if omitted. Other validators can remain on pull.
Do not have both collectors send the same node's Summit/Reth series. Arrange the
cutover to stop central pulling before starting the agent; temporary missing
telemetry alerts are possible during the transition.

The monitoring deployment generates expected agent, Summit, and Reth streams.
Missing data is eligible after a 2-minute freshness window and alerts after an
additional 2 minutes continuously missing (plus evaluation/notification delay).
A never-connected expected node alerts after the 2-minute pending period. Fresh
`up=0` uses the existing scrape-failure alerts instead. Observer failures notify
Slack but do not contribute to validator fleet counts.

## Verification

From the repository root:

```bash
python3 -m unittest discover -s tests -p 'test_prometheus_agent*.py' -v
```

To additionally test the actual pinned binary, provide locally downloaded,
checksum-verified executables:

```bash
PROMETHEUS_AGENT_BIN=/path/to/prometheus-3.5.0.linux-amd64/prometheus \
PROMETHEUS_AGENT_PROMTOOL=/path/to/prometheus-3.5.0.linux-amd64/promtool \
python3 -m unittest discover -s tests -p 'test_prometheus_agent*.py' -v
```

The opt-in integration test uses ephemeral loopback ports, a temporary trusted
TLS certificate, fake exporters, and a temporary Prometheus receiver. It does
not install software, invoke Supervisor, or contact live nodes. Monitoring-side
push inventory and alert tests live in the deploy repository.
