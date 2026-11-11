# Scoop Bucket

Personal [Scoop](https://scoop.sh) bucket for Windows apps.

## Install

```
scoop bucket add luestr https://github.com/luestr/scoop-manifest
scoop install luestr/<app>
```

## Apps

| Manifest | Description |
|---|---|
| `abacus-cli` | Terminal-based coding agent that reads code, edits files and runs commands in your repository |
| `Adguard` | System-wide ad blocker, tracker filter and privacy guard for Windows |
| `cherry-studio` | Desktop AI workspace for chatting with multiple model providers, running agents and managing a local knowledge base |
| `Clippi-Desktop` | A lightweight clipboard manager built with Rust and GPUI |
| `Finch` | An agent client for local execution, memory and desktop workflows |
| `Flectar-Mail` | A fast native mail client with instant startup and a small memory footprint |
| `Git-Agent` | A native desktop Git client for reviewing changes, browsing history and resolving conflicts |
| `github-stars-manager` | AI-powered manager for GitHub starred repositories with semantic search, auto-categorization and release tracking |
| `grit` | Reimplementing Git in Rust (CLI) |
| `JCode-Desktop` | Native spatial desktop client for Jcode, a coding agent driven by Claude or ChatGPT subscriptions |
| `kitter-desktop` | A simple, lightweight skill manager built in Rust |
| `mfiles` | Transfer files, sync the clipboard and manage files between a PC and phones or Macs over the local network |
| `Pulse` | A modern file manager for Windows with tabs, search, previews and shell integration |
| `sniffy-desktop` | An open-source traffic capture and debugging proxy for HTTP/HTTPS, WebSocket, gRPC and SSE |
| `tty7-Desktop` | A terminal workbench in pure Rust: shells, persistent sessions, SSH and coding agents |
| `tty7-Desktop-nightly` | A terminal workbench in pure Rust (nightly build) |
| `UnBlock` | Find and unlock files stuck in 'File in Use' or 'Access Denied' on Windows |
| `UU-Remote` | NetEase UU Remote: remote desktop and remote game streaming from any device |
| `Wu-Desktop` | The fast, native code editor and agent workspace |
| `yapfast-nightly` | A Discord client without the web browser, native and written in Rust (nightly) |
| `yapfast-releases` | A Discord client without the web browser, native and written in Rust |
| `WorkBuddy-CN` | AI agent for everyday office work that turns a single sentence into finished reports, decks and spreadsheets |
| `Zen-Desktop` | System-wide ad-blocker and privacy guard for Windows, macOS and Linux |

## Keeping manifests up to date

This repository holds manifests and the `bin/` wrappers. The updater lives in a
separate project, [scoop-manifest-tool](https://github.com/luestr/scoop-manifest-tool):
a stdlib-only Python 3.8+ tool that walks `bucket/*.json`, resolves each
manifest's newest upstream version through its `checkver` block, rewrites
`version`, `url`, `hash` and any other version-dependent field (`extract_dir`,
`notes`, …) from the `autoupdate` block, then commits and pushes the result.
The GitHub-specific lookup rules (latest tag, explicit tag, asset URLs and
SHA-256 sources) are documented in that project's `docs/github-release.md`.

Clone the tool next to this repository and point its `scoop_maintainer.toml` at
this checkout:

```pwsh
python ../scoop-manifest-tool/scoop_maintainer.py      # update, commit, push
```

The script takes no arguments; repositories, tokens and behaviour come from
`scoop_maintainer.toml`. When `force_push` is enabled (the default), the local
branch is authoritative: every push uses `--force` and replaces the remote
branch, regardless of its contents or whether it exists. The tool squashes
unpublished commits, so work from a fresh checkout and expect local commit
history to be rewritten.

`bin/` carries the usual BucketTemplate wrappers, so Scoop's upstream tooling
keeps working too:

```pwsh
.\bin\checkver.ps1 -u          # Scoop's own checkver/autoupdate
.\bin\checkhashes.ps1 -Update  # repair hashes only
.\bin\formatjson.ps1           # normalize JSON formatting
```

## Contributing

Read the [Contributing Guide](https://github.com/ScoopInstaller/.github/blob/main/.github/CONTRIBUTING.md)
and the [App Manifests](https://github.com/ScoopInstaller/Scoop/wiki/App-Manifests) wiki page.
