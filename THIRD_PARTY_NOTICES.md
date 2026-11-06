# Third-party notices

Ork Meter for Codex is proprietary software of Enterprise DevOps Solutions
LLC ("EDOS"), licensed under [LICENSE](LICENSE). Third-party components are
not part of the EDOS-owned Software; each remains under its own licence.
This repository holds only the installer, which uses the Python standard
library. The installed package and the helper each carry their own notices:

- the `ork-meter-codex` wheel includes the OpenAI `o200k_base` vocabulary
  and tiktoken's splitting pattern (MIT; `ork_codex/data/LICENSE-tiktoken.txt`
  inside the package), carries LICENSE, its full third-party notices and that
  tokenizer licence under `ork_meter_codex-<version>.dist-info/licenses/`,
  and installs `tiktoken` (MIT), `keyring` (MIT), `aiohttp` (Apache-2.0 AND
  MIT) and `tomlkit` (MIT) from PyPI, each with its own licence;
- the Ork Meter helper's Go module licences are in
  `ork-meter-THIRD_PARTY_NOTICES.txt`, and its EDOS licence in
  `ork-meter-codex-LICENSE.txt`, beside it in the release directory,
  `https://orkmeter.com/releases/codex/<version>/`; setup installs both next
  to the helper on your machine.
