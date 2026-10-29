# Installer

`install.sh` installs a Seismic validator or observer from a prepared
`node.env`. It has no interactive mode: node-specific choices are keys in the
config file, validated before touching the host. The `logging` step additionally
installs the fixed host-wide journal policy described below.

```bash
cp install/templates/node.env.example node.env   # edit: see docs/RUNBOOK.md 3.6
sudo install/install.sh --config node.env --dry-run
sudo install/install.sh --config node.env
```

| Option            | Effect                                                                  |
| ----------------- | ----------------------------------------------------------------------- |
| `--config PATH`   | Required. The `node.env` to install.                                    |
| `--dry-run`       | Validate the config and print the plan; change nothing. No root needed. |
| `--only STEP,...` | Run only these steps, always in canonical order.                        |

The steps, each implementing one section of the runbook:

| Step           | Runbook | Does                                                                     |
| -------------- | ------- | ------------------------------------------------------------------------ |
| `packages`     | 3.1     | apt packages; GitHub's `gh` repository for `download` installs           |
| `account`      | 3.2     | the `seismic` system account                                             |
| `directories`  | 3.3     | config, data, key, Custodian, and checkpoint directories                 |
| `binaries`     | 3.4     | per component: `download` (attested release), `source`, or `prebuilt`    |
| `runner`       | 3.5     | the checkpoint-start runner                                              |
| `config`       | 3.6     | `/etc/seismic/node.env` and the genesis file                             |
| `keys`         | 3.7     | Reth and Summit keys; existing keys are preserved, never regenerated     |
| `checkpointer` | 3.8     | `seismic-checkpointer.toml`, when enabled                                |
| `units`        | 3.10    | the systemd units and hardening drop-in; nothing enabled                 |
| `tls`          | 3.11    | managed OpenResty for `TLS_MODE=custodian-only` or `full`; not activated |
| `logging`      | 3.13    | shared, bounded host-wide journal policy; journald is not restarted      |

Every step is idempotent and says what it changed. Nothing is ever started:
first start is `seismic-node … start` (runbook section 4), and the Prometheus
Agent is `seismic-node monitoring install` (3.12), because both need inputs that
do not belong in `node.env`.

What a finished host looks like, how to start it, and how to operate it is
**[docs/RUNBOOK.md](../docs/RUNBOOK.md)**. Proxy requirements for operators
running their own TLS terminator are in [CUSTODIAN_TLS.md](CUSTODIAN_TLS.md).

## Host-wide logging policy

The `logging` step installs `templates/journald/zz-seismic-retention.conf` to
`/etc/systemd/journald.conf.d/zz-seismic-retention.conf` (`root:root`, `0644`).
It applies to **all services sharing the default system journal**, not just
Seismic: a shared approximately 2 GiB disk budget, 128 MiB journal files, a 5
GiB free-space allowance, and seven-day maximum retention. High log volume can
evict history sooner. It disables journal forwarding to syslog to avoid an
independently growing `/var/log/syslog`; syslog-based collectors, such as an Ops
Agent configured to tail that file, must be reviewed first.

The installer does not restart journald, vacuum journals, truncate text logs, or
restart node services. Activation requires an explicit
`sudo systemctl restart systemd-journald`; the new retention policy can then
remove old archived journals automatically. Review the merged settings first
with `sudo systemd-analyze cat-config systemd/journald.conf`. The `zz-` prefix
sorts after Ubuntu's vendor `syslog.conf`; a later local drop-in can still
override it. An rsyslog `imjournal` input would independently read journals and
also needs review.

To install only this policy without touching binaries, keys, node configuration,
or node service state:

```bash
sudo install/install.sh --config /etc/seismic/node.env --only logging
```

Then follow [runbook 3.13](../docs/RUNBOOK.md#313-bounded-system-logging) for
activation and checks. Syslog text files and direct application file logs,
including Summit critical logs, OpenResty logs, and the checkpointer's Reth
cache logs, are outside the journal budget and need their own retention
policies. For hosts whose logging is managed elsewhere, select only the other
installer steps with `--only`; an unrestricted rerun restores this repository's
policy file.

## Layout

| Path                         | Contents                                                    |
| ---------------------------- | ----------------------------------------------------------- |
| `install.sh`                 | the driver: argument parsing, validation, plan, step order  |
| `lib/steps.sh`               | one function per step                                       |
| `lib/binaries.sh`            | source checkout, Rust toolchain, and binary-install helpers |
| `lib/openresty.sh`           | OpenResty package, Lua libraries, rendering, JWT secret     |
| `lib/common.sh`              | output helpers                                              |
| `templates/node.env.example` | the config schema, with every key documented                |
| `templates/systemd/`         | the units, installed verbatim                               |
| `templates/journald/`        | the fixed host-wide journal retention drop-in               |
| `templates/config/`          | `${VAR}` templates rendered from `node.env`                 |
| `templates/openresty/`       | nginx and Lua templates                                     |

## Testing

`tests/test_installer.py` exercises `--dry-run` and
`tests/test_custodian_tls.py` the OpenResty rendering, both unprivileged.
`tests/test_journald.py` exercises the real logging step in a temporary tree,
reruns, symlink refusal, vendor drop-in ordering, and the documented manual
commands; no test changes host logging or starts services. `tests/vm/run.sh`
runs the real installer on a disposable VM over SSH:

```bash
VM_HOST=user@host VM_SSH_KEY=~/.ssh/key tests/vm/run.sh [--config FILE] [-- INSTALLER_ARGS]
```

Without `--config`, the harness generates a test-only validator configuration:
chain ID `5124`, reserved domain `seismic-vm.invalid`, and
`MONITORING_MODE=off`. No monitoring name, remote-write URL, or token is needed.
This exercises installation, not public certificate issuance or telemetry;
services are not started. Use `--config FILE` with real settings for those
checks. Supplied files are uploaded unchanged.

The harness needs Python 3 locally and validates the config before any sync,
upload, or SSH command. `tests/test_vm_harness.py` exercises this flow with
stubbed transports, without contacting a VM.

### Deployment validation (disposable Ubuntu 24.04 VM)

The offline tests and install-only harness do **not** validate actual systemd
sandboxing or application readiness. Before deployment, use an explicitly agreed
disposable Ubuntu 24.04 target, never a production node or the developer host,
and verify:

- Fresh validator and observer installations; each role's start, stop, restart,
  and installer rerun. Observer startup needs a test parent and provisioned
  parent node key (runbook 3.7); the generated harness config covers neither.
- Source-built Reth with `CHECKPOINTER_ENABLED=true`: `mdbx_copy` is installed
  and source checkout/build reruns preserve genuine local edits.
- The journal has no sandbox-related failures: interface discovery with
  `AF_NETLINK`, the syscall filter with jemalloc, and writes to configured data,
  key, socket, and checkpoint paths all work.
- Runtime paths chosen by the operator are accessible with `ProtectHome=yes`; do
  not put binaries, configuration, or state under `/home` without checking them.
  A source-build Rust toolchain is an installer input, not a runtime path.
- Summit exports working disk metrics (including its `/proc/mounts` access);
  scrape Summit at `http://127.0.0.1:9090/` and Reth at
  `http://127.0.0.1:9001/metrics` as in runbook section 4.
- `Type=simple` start success is not treated as readiness: inspect journals and
  application health after startup, not just the immediate unit state.
- After explicit journal activation, the effective limits match the template,
  fresh node logs remain visible, syslog no longer duplicates them, and node
  service PIDs/start times remain unchanged. Check `journalctl --disk-usage` and
  journal restart diagnostics for the shared budget.

Record the VM image, config, tested revisions, commands, and results. These
checks remain deployment prerequisites until they have actually run.
