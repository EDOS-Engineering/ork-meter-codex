# Third-party notices

Ork Meter for Codex is licensed by EDOS under a limited-use licence (see
[LICENSE](LICENSE)). It
includes or depends on the third-party components below, each under its own
licence.

## Included in this repository

| Component | Where | Licence |
| --- | --- | --- |
| OpenAI `o200k_base` vocabulary, compressed | `ork_codex/data/o200k_base.tiktoken.gz` | MIT ([`ork_codex/data/LICENSE-tiktoken.txt`](ork_codex/data/LICENSE-tiktoken.txt)) |
| tiktoken 0.9.0 splitting pattern and special-token definitions | `ork_codex/tokens.py` | MIT ([`ork_codex/data/LICENSE-tiktoken.txt`](ork_codex/data/LICENSE-tiktoken.txt)) |

Provenance and the vocabulary's SHA-256 are in
[`ork_codex/data/README.md`](ork_codex/data/README.md).

## Installed from PyPI, not included

| Package | Used by | Licence |
| --- | --- | --- |
| `tiktoken` | runtime | MIT |
| `keyring` | runtime | MIT |
| `aiohttp` | tests (optional `test` extra) | Apache-2.0 |
| `websockets` | tests (optional `test` extra) | BSD-3-Clause |

Each package's licence ships with the package you install.
