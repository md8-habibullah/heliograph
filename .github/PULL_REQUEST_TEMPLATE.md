## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] `python3 -X dev -m unittest discover -s tests -t tests` passes (and `make check-web` for interface changes)
- [ ] I added or updated tests, including a malformed-input case for anything that parses input
- [ ] I kept to [AGENTS.md](../AGENTS.md): standard library only, no network calls, no `innerHTML`, FFmpeg only through whitelisted presets and argument lists
- [ ] Work is done only for what is on screen (or is governed and low priority), and I did not add work that grows with library size on a hot path
- [ ] Docs and `CHANGELOG.md` are updated
- [ ] No personal data (real paths, names, e-mail addresses, library contents) in code, tests, docs or screenshots

## How I tested it

<!-- Commands, and for interface changes what you looked at. For performance changes: library size and the numbers before and after. -->
