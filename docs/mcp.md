# Heliograph for AI agents (MCP)

`heliograph mcp` runs a [Model Context Protocol](https://modelcontextprotocol.io) server on standard input and output, so an AI assistant can search and inspect your media library. It is built with the Python standard library only.

By default it is **read-only**. It never exposes file contents, never runs commands, and only names files that are in your index.

## Connect an assistant

Claude Code:

```bash
claude mcp add heliograph -- heliograph mcp
```

Claude Desktop, Cursor and most other clients take a JSON entry:

```json
{
  "mcpServers": {
    "heliograph": { "command": "heliograph", "args": ["mcp"] }
  }
}
```

From a source checkout use the full path: `"command": "/path/to/heliograph/bin/heliograph"`.

To let the assistant queue conversions, add `--allow-convert` (see the safety notes below):

```json
{ "mcpServers": { "heliograph": { "command": "heliograph", "args": ["mcp", "--allow-convert"] } } }
```

## Tools

| Tool | What it does |
| --- | --- |
| `library_status` | Counts of pictures, videos and audio, the indexed folders, whether the service is running |
| `search_media` | Words must all appear in the name, folder, title, artist or album. Filters: `kind`, `folder`, `year`, `month`, `sort` (`newest`, `oldest`, `name`, `largest`), `limit` (at most 200), `include_opened` |
| `list_folders` | Sub-folders of a library folder with picture, video and audio counts (whole subtree) |
| `media_info` | One indexed file: size, dates, dimensions, duration, tags, and via `ffprobe` its container, codecs, audio tracks, subtitles, and how the app would play it |
| `list_conversion_presets` | The 22 conversion presets with their ids |
| `conversion_jobs` | Recent conversion jobs with status and progress |
| `convert_media` | **Only with `--allow-convert`.** Queue conversions of indexed files with a preset; originals are never modified |

Every tool result is JSON text. Errors are returned as tool errors (`isError: true`) with a message an assistant can act on, for example "There is no library yet. Ask the user to open a folder in Heliograph".

## Examples

> "How many videos do I have from 2019, and which are the largest?"

`search_media { "kind": "video", "year": 2019, "sort": "largest", "limit": 10 }`

> "Convert the three biggest MOV files in my Trips folder to MP4 at 720p."

`search_media` to find them, then `convert_media { "paths": [...], "preset": "vid-mp4-h264", "max_height": 720 }` (needs `--allow-convert`), then `conversion_jobs` to follow progress.

## Safety model

- The database is opened **read-only**, so a bug in a tool cannot change your library.
- **Paths must be in the index.** `media_info` and `convert_media` refuse any path that is not an indexed file; `search_media` and `list_folders` refuse folders outside the library. `../` tricks and `/etc/passwd` are tested.
- **Search text is data.** It is matched literally (`instr`), never used in SQL or a shell.
- **File names, folders and tags come from your disk and may contain text that looks like instructions.** Tool descriptions tell the model to treat them as data. Clients should too.
- **Conversion is opt-in** and limited to the whitelisted presets, indexed files and validated options. It goes through the running service, so the same rules as the app apply (low priority, one job at a time, no overwriting).
- Results are capped (200 files per search).
- Nothing is sent anywhere by Heliograph. What your assistant does with the results is up to the assistant.

## Notes for client authors

- Transport: newline-delimited JSON-RPC 2.0 on stdio. Log lines go to stderr only.
- Implemented methods: `initialize`, `notifications/*`, `ping`, `tools/list`, `tools/call`. No resources, prompts or sampling.
- The protocol version in `initialize` is echoed back; the server speaks the 2024-11-05 tool format.
- The server needs the database to exist (`heliograph --add FOLDER` creates it). It does not need the service for read-only tools.
