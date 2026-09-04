# `seismic_node` internal package

This directory contains the implementation behind the operator-facing command:

```text
tools/seismic-node.py
```

Operators should invoke the executable rather than running these modules
directly. The package separates network acquisition, checkpoint transactions,
validator policy, observer policy, and Supervisor control so each safety
boundary can be reviewed and tested independently.

## Module map

| Module          | Responsibility                                                                                                                                                                                                     |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `checkpoint.py` | Validates inventories, manifests, archives, and weak-subjectivity files; installs and rolls back matching Reth/Summit state; validates installed checkpoint inputs; deletes explicitly confirmed rollback backups. |
| `download.py`   | Selects a completed checkpoint epoch, polls remote manifests, downloads verified archives, and resolves local, URL, or Summit-RPC weak-subjectivity anchors.                                                       |
| `rpc.py`        | Provides strict standard-library HTTP and JSON-RPC helpers with URL, redirect, response-size, token-file, archive-size, and SHA-256 checks.                                                                        |
| `supervisor.py` | Starts selected Supervisor programs in dependency order and reverses only the start requests made by the current command after a partial failure.                                                                  |
| `validator.py`  | Generates the exact deposit-signature response, applies validator lifecycle policy before checkpoint startup, and coordinates lifecycle-free validator restarts and shutdown.                                      |
| `observer.py`   | Selects normal or checkpoint observer startup and coordinates observer shutdown without applying validator lifecycle rules.                                                                                        |

`__init__.py` only identifies the internal package. It intentionally performs no
startup work or global configuration.

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

1. Validate the role-specific installation inventory and persistent identity.
2. Validate the manifest, archive layout, embedded metadata, finalized-header
   continuity, and weak-subjectivity age.
3. Require Reth, Summit, checkpoint, staging, and rollback paths to share a
   filesystem.
4. Require all relevant Supervisor programs to be stopped.
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

`checkpoint rollback` accepts an explicit `--backup` directory. When the option
is omitted in interactive use, the command lists the backups found under the
default rollback roots (derived from the installed inventories) newest first,
with each receipt's role, epoch, and state, and asks for a selection by number;
nothing is ever selected automatically, and non-interactive use requires
`--backup`.

### Validator onboarding

The validator flow uses the node public key from a strictly validated
`deposit-signature.json` response to call `getValidatorAccount`.

- `Joining` starts normally.
- `Active` starts with a late-onboarding warning.
- `NotFound` and `Inactive` require a wait, explicit early-start decision, or a
  leave-stopped decision.
- `SubmittedExitRequest`, `FullPayoutPending`, malformed responses, and unknown
  statuses are refused.

The lifecycle state is checked before preparation and again immediately before
startup. A confirmed early-start decision is carried across the second check so
an interactive operator is not prompted twice.

### Supervisor startup

Observer startup, validator startup (`validator start` and authorized
`validator onboard` startup), and deposit-signature generation first run
`systemctl enable --now supervisor`, followed by `supervisorctl reread` and
`supervisorctl update`. Startup then calls `supervisor.start_node()`, which
starts dependencies in this order:

1. `custodian`, when configured.
2. `reth`.
3. The selected normal or checkpoint Summit program.
4. `checkpointer`, when configured.

The conflicting Summit program and `summit-deposit-rpc` must be stopped. Once
Supervisor accepts a start request, that program is recorded immediately. If a
later program fails or times out, recorded programs are stopped in reverse
order. Programs that were already running before the command are not stopped.

### Supervisor shutdown

`observer stop` and `validator stop` validate their role-specific installation
inventories and call `supervisor.stop_node()` to stop programs in reverse
dependency order:

1. `checkpointer`, when configured.
2. The role's deposit-signature, normal, and checkpoint Summit programs.
3. `reth`.
4. `custodian`, when configured.

Missing and already-stopped programs are skipped. A stop failure aborts before
lower-level dependencies are stopped. Supervisor itself and OpenResty remain
running.

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

Package modules raise one of these operator-facing exceptions:

- `checkpoint.CheckpointError`
- `rpc.NetworkError`
- `supervisor.SupervisorError`

`tools/seismic-node.py` catches them, prints a concise error, and exits with
status 1. Unexpected programming errors are not hidden behind a generic catch.
Keyboard interruption exits with status 130.

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
deposit-signature requests, Supervisor startup cleanup, interrupted rollback
behavior, and checkpoint-runner locking.
