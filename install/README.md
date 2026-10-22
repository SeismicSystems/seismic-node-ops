# Installer

`install.sh` installs a Seismic validator or observer from a prepared
`node.env`. It has no interactive mode: every decision is a key in the config
file, which it validates before touching the host and installs unchanged as
`/etc/seismic/node.env`.

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

| Step           | Runbook | Does                                                                              |
| -------------- | ------- | --------------------------------------------------------------------------------- |
| `packages`     | 3.1     | apt packages; GitHub's `gh` repository for `download` installs                    |
| `account`      | 3.2     | the `seismic` system account                                                      |
| `directories`  | 3.3     | config, data, key, Custodian, and checkpoint directories                          |
| `binaries`     | 3.4     | per component: `download` (attested release), `source`, `prebuilt`, or `deferred` |
| `runner`       | 3.5     | the checkpoint-start runner                                                       |
| `config`       | 3.6     | `/etc/seismic/node.env` and the genesis file                                      |
| `keys`         | 3.7     | Reth and Summit keys; existing keys are preserved, never regenerated              |
| `checkpointer` | 3.8     | `seismic-checkpointer.toml`, when enabled                                         |
| `units`        | 3.10    | the systemd units and hardening drop-in; nothing enabled                          |
| `tls`          | 3.11    | managed OpenResty for `TLS_MODE=custodian-only` or `full`; not activated          |

Every step is idempotent and says what it changed. Nothing is ever started:
first start is `seismic-node … start` (runbook section 4), and the Prometheus
Agent is `seismic-node monitoring install` (3.12), because both need inputs that
do not belong in `node.env`.

What a finished host looks like, how to start it, and how to operate it is
**[docs/RUNBOOK.md](../docs/RUNBOOK.md)**. Proxy requirements for operators
running their own TLS terminator are in [CUSTODIAN_TLS.md](CUSTODIAN_TLS.md).

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
| `templates/config/`          | `${VAR}` templates rendered from `node.env`                 |
| `templates/openresty/`       | nginx and Lua templates                                     |

## Testing

`tests/test_installer.py` exercises `--dry-run` and
`tests/test_custodian_tls.py` the OpenResty rendering, both unprivileged.
`tests/vm/run.sh` runs the real installer on a disposable VM over SSH:

```bash
VM_HOST=user@host VM_SSH_KEY=~/.ssh/key tests/vm/run.sh [--config FILE] [-- INSTALLER_ARGS]
```
