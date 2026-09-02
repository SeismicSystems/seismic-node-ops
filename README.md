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

Generate bearer tokens for OpenResty-protected endpoints with:

```text
tools/generate-openresty-jwt.sh
```

Install or roll back a verified validator or observer checkpoint with:

```text
tools/checkpoint-start/install-checkpoint.py
```

The Summit internal-testnet genesis file is provided at:

```text
internal_testnet_genesis.toml
```

## Repository checks

GitHub Actions checks shell scripts and Markdown through
[`.github/workflows/checks.yml`](.github/workflows/checks.yml).

Run the same checks locally from the repository root. ShellCheck must be
installed; Go and Node.js with `npx` are also required.

```bash
go run mvdan.cc/sh/v3/cmd/shfmt@v3.12.0 -d install tools

(
  cd install
  shellcheck -x \
    install-validator.sh \
    install-observer.sh \
    ../tools/generate-openresty-jwt.sh
)

python3 -m py_compile \
  tools/checkpoint-start/install-checkpoint.py \
  tools/checkpoint-start/summit-checkpoint-runner.py
python3 -m unittest discover -s tests -p 'test_*.py' -v

npx --yes prettier@3.6.2 --check '**/*.md'
npx --yes markdownlint-cli2@0.21.0 './**/*.md' '#./.git/**'
```

Shell formatting is defined in [`.editorconfig`](.editorconfig). Markdown
formatting and lint rules are defined in [`.prettierrc.json`](.prettierrc.json)
and [`.markdownlint-cli2.jsonc`](.markdownlint-cli2.jsonc).
