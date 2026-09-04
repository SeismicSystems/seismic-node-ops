# Seismic Node Operations

Operational tooling and configuration for installing and running Seismic
validator and observer nodes.

## Validator installation

The interactive validator installer is located at:

```text
install/install-validator.sh
```

For requirements, installation steps, persistent storage, MDBX notes, validator
registration, service startup, OpenResty, and troubleshooting, see the
**[Validator Installer and First-Start Guide](install/README.md)**.

## Observer installation

The observer installer is located at:

```text
install/install-observer.sh
```

See the **[Observer Installer and First-Start Guide](install/OBSERVER.md)**.

## Node onboarding and operations

After installation, one CLI downloads and installs checkpoints, creates
validator deposit signatures, and starts and stops validator or observer
services:

```text
tools/seismic-node.py
```

Its commands are `checkpoint install`, `checkpoint rollback`,
`checkpoint delete-backup`, `validator deposit-signature`, `validator onboard`,
`validator start`, `validator stop`, `observer start`, and `observer stop`. The
role guides show the onboarding workflows; implementation and security notes are
in [`tools/seismic_node/README.md`](tools/seismic_node/README.md).

Generate bearer tokens for OpenResty-protected endpoints with:

```text
tools/generate-openresty-jwt.sh
```

The Summit internal-testnet genesis file is provided at:

```text
internal_testnet_genesis.toml
```

## Repository checks

GitHub Actions checks shell scripts, Python, and Markdown through
[`.github/workflows/checks.yml`](.github/workflows/checks.yml).

Run the same checks locally from the repository root. ShellCheck must be
installed; Go, Node.js with `npx`, Python 3.12, and `uvx` are also required.
`uvx` runs the pinned Ruff version only for development checks and is not
installed on node hosts.

```bash
go run mvdan.cc/sh/v3/cmd/shfmt@v3.12.0 -d install tools

(
  cd install
  shellcheck -x \
    install-validator.sh \
    install-observer.sh \
    ../tools/generate-openresty-jwt.sh
)

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

npx --yes prettier@3.6.2 --check '**/*.md'
npx --yes markdownlint-cli2@0.21.0 './**/*.md' '#./.git/**'
```

Shell formatting is defined in [`.editorconfig`](.editorconfig). Markdown
formatting and lint rules are defined in [`.prettierrc.json`](.prettierrc.json)
and [`.markdownlint-cli2.jsonc`](.markdownlint-cli2.jsonc).
