# Contributing to Heliograph

Thank you for helping. Bug reports with a file that does not play, a format that is missing, or a measurement from your own library are as valuable as code.

## Ground rules

- Be kind; see the [Code of Conduct](CODE_OF_CONDUCT.md).
- The project rules in [AGENTS.md](AGENTS.md) apply to humans too: standard library only, no network features, no `innerHTML`, FFmpeg only through argument lists and whitelisted presets, work only for what is on screen. Pull requests that break them will be asked to change.
- Never put personal data in an issue, pull request, test or screenshot: replace real paths, names and e-mail addresses with placeholders.

## Set up

```bash
git clone https://github.com/md8-habibullah/heliograph.git
cd heliograph
python3 -X dev -m unittest discover -s tests -t tests     # should pass in about 12 s
./bin/heliograph --help
```

You need Python 3.10+. For the full test suite also install `ffmpeg` and Pillow (`pip install Pillow`); tests that need them skip themselves otherwise. `make check-web` needs Node.js (only for `node --check`). Linters are optional: `ruff` is configured in `pyproject.toml`.

**Use a scratch home when you run the app while developing**, so your real library is never touched:

```bash
export HELIOGRAPH_HOME="$(mktemp -d)"
./bin/heliograph --add ~/some/test/folder
```

## Making a change

1. Open an issue first for anything big, so the direction is agreed.
2. Branch from `main`. Keep the change focused.
3. Add or update tests. Tests use `tests/helpers.py: IsolatedHome` (own home and library folder per test).
4. Run the whole suite and, for interface changes, `make check-web` and look at the result in a browser against a scratch library.
5. Update the docs that describe the behaviour and add a line to `CHANGELOG.md` under *Unreleased*.
6. Open a pull request and fill in the template.

### Where things are

[docs/ai/ARCHITECTURE.md](docs/ai/ARCHITECTURE.md) maps the code and has recipes for the common changes: add a format, a conversion preset, a setting, an API route, a schema migration or an MCP tool.

### Performance changes

If your change touches scanning, views, thumbnails or the grid, say what you measured. [docs/performance.md](docs/performance.md) describes the 14,000-file and million-row procedure (`tools/synth_library.py`, `tools/scale_probe.py`).

## Reporting bugs

Include: `heliograph --version`, your distribution and browser, the output of `heliograph doctor`, and for a media problem the output of `heliograph info FILE` (check it for personal paths first). For security problems see [SECURITY.md](SECURITY.md).

## Commit and review

Use clear, imperative commit messages ("Skip TypeScript files named .ts"). Maintainers may squash. There is no CLA; contributions are under the [MIT license](LICENSE).
