# Third-party notices

Ork Meter for Codex is licensed by EDOS under a limited-use licence (see
[LICENSE](LICENSE)). This repository holds only the installer, which uses
the Python standard library. The installed package and the helper each
carry their own notices:

- the `ork-meter-codex` wheel includes the OpenAI `o200k_base` vocabulary
  and tiktoken's splitting pattern (MIT; `ork_codex/data/LICENSE-tiktoken.txt`
  inside the package) and installs `tiktoken` (MIT) and `keyring` (MIT)
  from PyPI;
- the Ork Meter helper's Go module licences are in
  `ork-meter-THIRD_PARTY_NOTICES.txt` beside it in the release directory,
  `https://orkmeter.com/releases/codex/<version>/`.
