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
| `cherry-studio` | Desktop AI workspace for chatting with multiple model providers, running agents and managing a local knowledge base |
| `github-stars-manager` | AI-powered manager for GitHub starred repositories with semantic search, auto-categorization and release tracking |
| `kitter-desktop` | A simple, lightweight skill manager built in Rust |
| `sniffy-desktop` | An open-source traffic capture and debugging proxy for HTTP/HTTPS, WebSocket, gRPC and SSE |
| `Wu-Desktop` | The fast, native code editor and agent workspace |
| `yapfast-nightly` | A Discord client without the web browser, native and written in Rust (nightly) |
| `yapfast-releases` | A Discord client without the web browser, native and written in Rust |
| `WorkBuddy-CN` | AI agent for everyday office work that turns a single sentence into finished reports, decks and spreadsheets |

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
