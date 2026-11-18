# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private reporting: on the repository page choose **Security → Report a vulnerability**. Include the version (`heliograph --version`), what you did, and what happened. You will get an acknowledgement, and a fix or a clear answer as soon as it can be investigated. Please give a reasonable time to fix a problem before disclosing it publicly.

Supported versions: the latest release (this is a young project; fixes go to the newest version).

## What Heliograph protects

Heliograph is a local application. Its threat model is:

1. **Other web pages in your browser** must not be able to read or control it. The service listens on `127.0.0.1` only, rejects requests whose `Host` is not `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding), rejects cross-site browser requests (`Sec-Fetch-Site`), and requires a random per-session token on every API call except `/api/ping`. The token is given to the page it serves and to the local command line (through a file readable only by your user).
2. **Other users on the same computer.** The database, caches, state and logs live in directories with mode `0700` and files with mode `0600`; the service runs with `umask 077`.
3. **Malicious media files and file names.** Files are served by database id from folders you opened, never by a path in the request; served files carry `Content-Security-Policy: sandbox` and `X-Content-Type-Options: nosniff`, so an SVG or HTML file cannot run script; the interface never writes untrusted text with `innerHTML`; file names are never placed in SQL or a shell.
4. **Command injection into FFmpeg and ImageMagick.** They are started with argument lists (never through a shell), with absolute source paths, and conversion accepts only a fixed list of presets with validated options.
5. **Folders that must never be served.** The system folders `/` itself, `/etc`, `/usr`, `/bin`, `/sbin`, `/lib`, `/var/lib`, `/boot`, `/dev`, `/proc`, `/sys` and `/run/user` cannot be opened, and neither can any folder your user is not allowed to read. Symbolic links that point outside the chosen folder are not indexed.
6. **AI agents.** The MCP server opens the database read-only, only names indexed files, and exposes conversion only when you start it with `--allow-convert`. See [docs/mcp.md](docs/mcp.md).

## What it does not protect against

- Anything running as your own user can read your files anyway, and can read the session token. Heliograph does not try to defend against malware running as you.
- Vulnerabilities in FFmpeg, ImageMagick, LibRaw, libheif, Pillow or your browser when they open a hostile file. Keep them updated. Heliograph runs them at low priority and with time limits, but it does not sandbox them.
- A hostile local user with root.

## Hardening you can do

- Keep your distribution's `ffmpeg`, `imagemagick` and browser packages up to date.
- Run `heliograph doctor` to check permissions and tools.
- Do not open untrusted folders from network shares you do not control.

## Development practice

The test suite includes checks for the token, the `Host` and cross-site guards, path handling, range requests, malformed parameters, SQL-looking search text, MCP path escapes, and unsafe DOM APIs in the interface. Contributions must keep these passing (see [AGENTS.md](AGENTS.md)).
