# `seismic_node` internal package

This directory contains the implementation behind the operator-facing command:

```text
tools/seismic-node.py
```

Operators should invoke the executable rather than running these modules
directly. The package separates network acquisition, checkpoint transactions,
validator policy, observer policy, and systemd control so each safety boundary
can be reviewed and tested independently.

Every command reads `/etc/seismic/node.env` (override with `--node-env`) for the
node's role, paths, and enabled features; see
`install/templates/node.env.example`.

## Module map

| Module          | Responsibility                                                                                                                                                                                                  |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `errors.py`     | Defines `NodeError`, the base class for every operator-facing failure.                                                                                                                                          |
| `fsutil.py`     | Validates paths and file ownership, and reads TOML/JSON files with `O_NOFOLLOW` and exact-schema checks; shared by every module.                                                                                |
| `nodeenv.py`    | Parses `/etc/seismic/node.env` with systemd `EnvironmentFile=` semantics and validates it against the node configuration schema.                                                                                |
| `checkpoint.py` | Validates node.env, manifests, archives, and weak-subjectivity files; installs and rolls back matching Reth/Summit state; validates installed checkpoint inputs; deletes explicitly confirmed rollback backups. |
| `download.py`   | Selects a completed checkpoint epoch, polls remote manifests, downloads verified archives, and resolves local, URL, or Summit-RPC weak-subjectivity anchors.                                                    |
| `rpc.py`        | Provides strict standard-library HTTP and JSON-RPC helpers with URL, redirect, response-size, token-file, archive-size, and SHA-256 checks.                                                                     |
| `systemd.py`    | Starts selected systemd units in dependency order and reverses only the start requests made by the current command after a partial failure.                                                                     |
| `agent.py`      | Installs the pinned Prometheus release and renders `prometheus.yml` from the `MONITORING_*` keys in `node.env`; never starts a service.                                                                         |
| `validator.py`  | Generates the exact deposit-signature response, applies validator lifecycle policy before normal or checkpoint startup, and coordinates lifecycle-free validator restarts and shutdown.                         |
| `observer.py`   | Selects normal or checkpoint observer startup and coordinates observer shutdown without applying validator lifecycle rules.                                                                                     |

`__init__.py` only identifies the internal package. It intentionally performs no
startup work or global configuration.

## Optional monitoring

`monitoring.py` manages the independent Prometheus Agent configured by the
runbook (section 3.12). A shared best-effort helper runs only after successful
validator/observer startup; onboarding authorization, identity checks,
checkpoint validation, and node-start rollback are unchanged. Invalid agent
metadata or agent startup failure warns without rolling back successful node
startup.

Node stop and rollback never stop the agent. Explicit commands:

```bash
sudo ./tools/seismic-node.py monitoring install --token-source /root/monitoring-token
sudo ./tools/seismic-node.py monitoring check
sudo ./tools/seismic-node.py monitoring start
sudo ./tools/seismic-node.py monitoring status
sudo ./tools/seismic-node.py monitoring stop
```

The agent is used in `MONITORING_MODE=push`, the validator default. A validator
pushes to Seismic (`--token-source` required) and optionally to the operator's
own Prometheus; an observer pushes to the operator's Prometheus only, with no
`--token-source`. `install` downloads the checksum-pinned Prometheus release,
renders `/etc/seismic/prometheus-agent/prometheus.yml` and `targets.json` from
the `MONITORING_*` and service keys in `node.env`, and copies the token from a
root-only (0600) handoff file; it never starts the service. With
`MONITORING_OPERATOR_REMOTE_WRITE_URL` set in `node.env` the rendered
configuration carries a second remote-write destination, the operator's own
Prometheus; `--operator-token-source` supplies its bearer token when needed, and
a reinstall without the flag removes it again. `check` verifies the installed
artifacts still match `node.env`. Monitoring commands read `node.env` without
requiring usable node databases or keys. To stop using the agent, set
`MONITORING_MODE` to `pull` or `off` and run `monitoring stop`; credentials and
the WAL are preserved.

## Workflow boundaries

### Checkpoint acquisition

`download.resolve_checkpoint_inputs()` is a context manager. It performs the
non-destructive part of onboarding:

1. Require exactly one local or remote checkpoint source.
2. Select a completed epoch without silently replacing an explicitly requested
   epoch.
3. Validate the snapshot manifest, fetched from the checkpoint-app provider
   endpoints `<base>/checkpoints/<epoch>/manifest` and
   `<base>/checkpoints/<epoch>/snapshot`.
4. Obtain a weak-subjectivity anchor from exactly one configured source.
5. Prefer an independently hosted anchor. If the remote anchor and snapshot
   provider have the same normalized URL origin, emit a strong warning and
   require interactive confirmation or the explicit
   `--allow-same-origin-weak-subjectivity` option.
6. Stream the archive into a root-only temporary directory and verify its size
   and SHA-256 while downloading.
7. Yield local paths to the checkpoint transaction and remove generated files
   afterward.

Local archive and manifest paths remain supported. Network acquisition does not
modify Reth or Summit state. Unattended workflows confirm destructive steps with
`--yes` and should pin `--checkpoint-epoch` with `--checkpoint-policy exact`,
keeping dynamic discovery from changing the epoch authorized by automation.

### Checkpoint transaction

`checkpoint.install_checkpoint()` performs the destructive part only after all
inputs have been acquired:

1. Validate `node.env` for the role and the persistent identity.
2. Validate the manifest, archive layout, embedded metadata, finalized-header
   continuity, and weak-subjectivity age.
3. Require Reth, Summit, checkpoint, staging, and rollback paths to share a
   filesystem.
4. Require all relevant systemd units to be stopped.
5. Acquire the same non-blocking lock used by normal and checkpoint Summit
   programs, then check service state again.
6. Create a root-only rollback directory and an `installing` receipt.
7. Atomically move the current Reth and complete mutable Summit state into the
   rollback directory. Summit keys and observer assignment stay outside this
   transaction.
8. Atomically move staged Reth and checkpoint state into place and write the
   checkpoint-start and weak-subjectivity TOML files.
9. Mark the receipt `installed` and leave all services stopped.

If installation fails after the rollback receipt exists, the code attempts a
conservative restoration. It does not delete state for which no corresponding
backup move was recorded.

`checkpoint rollback` and `checkpoint delete-backup` accept an explicit
`--backup` directory. When the option is omitted in interactive use, both
commands list the backups found under the default rollback roots (derived from
`node.env`) newest first, with each receipt's role, epoch, and state, and ask
for a selection by number; nothing is ever selected automatically, and
non-interactive use requires `--backup`.

### Validator onboarding

The validator flow reads `summit_keys_dir` from the validated installation
`node.env`, then invokes `/usr/local/bin/summit keys show --key-store-path DIR`
to derive the installed node public key. Only that public key is sent to
`getValidatorAccount`; private keys remain local and no deposit RPC is started.
The command is read-only, has a bounded runtime, and its raw output is never
included in errors. The parsed identity must be exactly one 32-byte ed25519
public key.

`--deposit-signature` is optional. When supplied, the response is strictly
validated and its node public key must match the installed identity before any
lifecycle polling or checkpoint installation. The installed identity is derived
again before startup and must still match the public key whose lifecycle was
checked. `--node-env` overrides the default `/etc/seismic/node.env` without
requiring a signature file.

- `Joining` starts normally.
- `Active` starts with a late-onboarding warning.
- `NotFound` and `Inactive` require a wait, explicit early-start decision, or a
  leave-stopped decision.
- `SubmittedExitRequest`, `FullPayoutPending`, malformed responses, and unknown
  statuses are refused.

`validator onboard --mode checkpoint` is the default: it installs a selected
checkpoint or validates existing checkpoint-start inputs before startup.
`validator onboard --mode normal` instead starts from local state without
installing or requiring a checkpoint. Normal mode rejects checkpoint sources and
installation modifiers, including `--yes`.

For lifecycle-gated normal startup:

```bash
sudo ./tools/seismic-node.py validator onboard \
  --mode normal \
  --summit-rpc-url https://trusted-validator.example/summit \
  --pre-joining-policy wait
```

Both modes validate `node.env` and apply the same lifecycle policy. The
lifecycle state is checked before preparation and again immediately before
startup. A confirmed early-start decision is carried across the second check so
an interactive operator is not prompted twice. One wait deadline spans both
checks; `--validator-wait-timeout 0` (the default) waits indefinitely. Normal
startup does not guarantee synchronization from empty state.
`validator start --mode normal` remains the lifecycle-free startup command.

### Service startup

Observer startup, validator startup (`validator start` and authorized
`validator onboard` startup), and deposit-signature generation first run
`systemctl daemon-reload` so freshly installed units are known. Startup then
calls `systemd.start_node()`, which starts units in this order:

1. `seismic-custodian` (`seismic-observer-custodian` for observers), when
   installed.
2. `seismic-reth`.
3. The selected normal or checkpoint Summit unit.
4. `seismic-checkpointer`, when installed.

The conflicting Summit unit and `seismic-deposit-rpc` must be stopped. A start
request is recorded before `systemctl start` is issued. If a later unit fails,
recorded units that are still running are stopped in reverse order. Units that
were already running before the command are not stopped. `systemctl start`
blocks until the unit is active or has failed, so no polling is needed.

### Service shutdown

`observer stop` and `validator stop` validate `node.env` and the installed
identity, then call `systemd.stop_node()` to stop units in reverse dependency
order:

1. `seismic-checkpointer`, when installed.
2. The role's deposit-signature, normal, and checkpoint Summit units.
3. `seismic-reth`.
4. The role's Custodian unit, when installed.

Missing and already-stopped units are skipped. A stop failure aborts before
lower-level dependencies are stopped. An administrative stop inhibits
`Restart=`, so a stopped unit stays stopped. OpenResty remains running.

## Security invariants

Keep these properties when changing the package:

- Non-loopback remote URLs must use HTTPS.
- URLs must not contain embedded credentials, and redirects remain disabled.
- Bearer tokens come from root-owned private files and are never command-line
  values.
- Independent snapshot and remote weak-subjectivity origins remain the
  recommended trust model. Same-origin use always requires a strong warning and
  explicit operator authorization. Origin comparison uses normalized URL scheme,
  hostname, and port; it cannot prove that different DNS names are operated by
  independent parties.
- Archive members remain restricted to the documented snapshot layout; links,
  traversal, duplicates, and unexpected roots are rejected.
- Checkpoint import always supplies both a checkpoint path and a
  weak-subjectivity path.
- Never introduce `--checkpoint-or-default`,
  `--unsafe-skip-checkpoint-verification`, or unanchored imports.
- Reth and Summit state are replaced and restored together with atomic
  same-filesystem renames.
- The complete mutable Summit data directory is backed up, excluding only the
  process-lock file. Summit keys remain outside replacement.
- Destructive commands require stopped services, a kernel lock, rollback assets,
  and explicit confirmation.
- Rollback backups are never deleted automatically.
- Normal and checkpoint Summit programs are never started together.

## Error handling

Package modules raise `errors.NodeError` or one of its subclasses:

- `nodeenv.NodeEnvError` for an unreadable or invalid `node.env`
- `rpc.NetworkError` (and `HttpStatusError`, `JsonRpcError`) for HTTP and
  JSON-RPC failures
- `systemd.ServiceError` for service-manager failures
- `agent.AgentError` for Prometheus Agent installation failures

`tools/seismic-node.py` catches `NodeError`, prints a concise error, and exits
with status 1. Unexpected programming errors are not hidden behind a generic
catch. Keyboard interruption exits with status 130.

## Tests and checks

CI uses Python 3.12, Ruff 0.16.5, Python compilation, and the unit tests. Run
the same checks from the repository root; developers with `uvx` can run the
pinned Ruff version without installing it into the project:

```bash
uvx --from ruff==0.16.5 ruff check --target-version py312 \
  tools/seismic-node.py \
  tools/seismic_node \
  tools/checkpoint-start/summit-checkpoint-runner.py \
  tests
uvx --from ruff==0.16.5 ruff format --check --target-version py312 \
  tools/seismic-node.py \
  tools/seismic_node \
  tools/checkpoint-start/summit-checkpoint-runner.py \
  tests
python3.12 -m py_compile \
  tools/seismic-node.py \
  tools/seismic_node/*.py \
  tools/checkpoint-start/summit-checkpoint-runner.py
python3.12 -m unittest discover -s tests -p 'test_*.py' -v
```

Tests cover remote acquisition, same-origin trust warnings and authorization,
archive validation, weak-subjectivity age, validator lifecycle decisions, exact
deposit-signature requests, service startup cleanup, interrupted rollback
behavior, and checkpoint-runner locking.
