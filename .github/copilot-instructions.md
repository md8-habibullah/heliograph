# Copilot instructions

Follow [AGENTS.md](../AGENTS.md) in the repository root; it is the single source of project rules.

Essentials: Python 3.10+ standard library only (no runtime dependencies); framework-free JavaScript with no `innerHTML`; the service listens on `127.0.0.1` only and has no network calls or telemetry; FFmpeg is only ever run with argument lists and whitelisted presets; originals are never modified; tests run in an isolated `HELIOGRAPH_HOME` (`python3 -m unittest discover -s tests -t tests`); do the least work for what is on screen.
