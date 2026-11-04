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
| `Alacritty-Portable` | A cross-platform, GPU-accelerated terminal emulator |
| `cherry-studio` | Desktop AI workspace for chatting with multiple model providers, running agents and managing a local knowledge base |
| `Clippi-Desktop` | A lightweight clipboard manager built with Rust and GPUI |
| `Finch` | An agent client for local execution, memory and desktop workflows |
| `Flectar-Mail` | A fast native mail client with instant startup and a small memory footprint |
| `Git-Agent` | A native desktop Git client for reviewing changes, browsing history and resolving conflicts |
| `github-stars-manager` | AI-powered manager for GitHub starred repositories with semantic search, auto-categorization and release tracking |
| `grit` | Reimplementing Git in Rust (CLI) |
| `JCode-Desktop` | Native spatial desktop client for Jcode, a coding agent driven by Claude or ChatGPT subscriptions |
| `kitter-desktop` | A simple, lightweight skill manager built in Rust |
| `Pulse` | A modern file manager for Windows with tabs, search, previews and shell integration |
| `sniffy-desktop` | An open-source traffic capture and debugging proxy for HTTP/HTTPS, WebSocket, gRPC and SSE |
| `tty7-Desktop` | A terminal workbench in pure Rust: shells, persistent sessions, SSH and coding agents |
| `tty7-Desktop-nightly` | A terminal workbench in pure Rust (nightly build) |
| `UnBlock` | Find and unlock files stuck in 'File in Use' or 'Access Denied' on Windows |
| `UU远程` | NetEase UU Remote: remote desktop and remote game streaming from any device |
| `Wu-Desktop` | The fast, native code editor and agent workspace |
| `yapfast-nightly` | A Discord client without the web browser, native and written in Rust (nightly) |
| `yapfast-releases` | A Discord client without the web browser, native and written in Rust |
| `WorkBuddy-CN` | AI agent for everyday office work that turns a single sentence into finished reports, decks and spreadsheets |
| `Zen-Desktop` | System-wide ad-blocker and privacy guard for Windows, macOS and Linux |

## Keeping manifests up to date

No CI required. `tools/update_manifests.py` is a single-file, stdlib-only
Python 3.8+ script that walks `bucket/*.json`, resolves each manifest's newest
upstream version through its `checkver` block, and rewrites `version`, `url`,
`hash` and any other version-dependent field (`extract_dir`, `notes`, …) from
the `autoupdate` block.

```pwsh
python tools\update_manifests.py                  # update everything
python tools\update_manifests.py -n               # dry run, report only
python tools\update_manifests.py kitter-desktop   # one app
python tools\update_manifests.py -f               # recompute even if unchanged
python tools\update_manifests.py --commit --push  # commit and push the result
```

Exit code is `0` when every manifest was handled (changed or already current)
and `1` when at least one failed, so it is safe to schedule:

```pwsh
schtasks /Create /SC HOURLY /TN "scoop-manifest update" /TR ^
  "python D:\path\to\scoop-manifest\tools\update_manifests.py --commit --push"
```

Notes:

- Versions are resolved the same way Scoop's own `checkver` does it — GitHub
  HTML pages, not the REST API — so there is no 60 req/h anonymous rate limit.
  Manifests that hardcode `api.github.com` fall back to equivalent HTML pages
  automatically. Export `GITHUB_TOKEN` to use the API with its 5000 req/h quota.
- Hashes come from `autoupdate.hash.url` when present, otherwise from the digest
  GitHub publishes on the release page. Downloading the asset to compute a hash
  is only the last resort; `--no-download` disables it.
- Rewrites are byte-stable: an unchanged manifest is never touched, and a
  round-trip reproduces the existing formatting exactly.

`bin/` additionally carries the usual BucketTemplate wrappers, so the upstream
tooling keeps working too:

```pwsh
.\bin\checkver.ps1 -u          # Scoop's own checkver/autoupdate
.\bin\checkhashes.ps1 -Update  # repair hashes only
.\bin\formatjson.ps1           # normalize JSON formatting
```

## Contributing

Read the [Contributing Guide](https://github.com/ScoopInstaller/.github/blob/main/.github/CONTRIBUTING.md)
and the [App Manifests](https://github.com/ScoopInstaller/Scoop/wiki/App-Manifests) wiki page.
