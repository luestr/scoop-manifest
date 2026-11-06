# Reading GitHub Releases

How to obtain the three values every Scoop manifest needs — **version**, **download URL**, and **SHA-256** — from a GitHub release, both for the latest release and for an explicitly named tag.

The rules below mirror `tools/update_manifests.py` and Scoop's own `checkver`/`autoupdate` implementation. Examples use [`what1f/kitter`](https://github.com/what1f/kitter), whose current tag is `v0.2.4` and whose desktop asset is `Kitter-0.2.4-desktop-windows-x86_64.exe`.

## At a glance

| Need | Best source | API quota |
|---|---|---|
| Latest version | `GET /releases/latest`, read the `/releases/tag/{tag}` redirect | none (HTML) |
| Version for a known tag | `GET /releases/tags/{tag}` → `tag_name` | 1 request |
| Download URL | `assets[].browser_download_url`, or `/releases/download/{tag}/{asset}` | none |
| SHA-256 | `assets[].digest`, or `releases/expanded_assets/{tag}` | 1 request / none |
| SHA-256 fallback | stream the asset through `hashlib.sha256` | none, but downloads the file |

Prefer a published digest. Downloading a 500 MB installer only to hash it is the slowest and least reliable option.

## 1. Resolve the latest version

### HTML route (no token, no API quota)

Request:

```text
GET https://github.com/{owner}/{repo}/releases/latest
```

GitHub answers with a `302` whose `Location` is the actual release page:

```text
https://github.com/what1f/kitter/releases/tag/v0.2.4
```

Follow the redirect and read the final URL, or read `Location` without following it. Extract the tag:

```text
/releases/tag/(?P<tag>[^/?#]+)
```

Then URL-decode it. `/releases/latest` returns the newest release that is neither a draft nor a pre-release.

Do **not** scrape the release page body for a version. It contains asset names, commit links, bundle hashes and JavaScript, and a loose `\d+\.\d+\.\d+` pattern can easily capture the wrong string. The redirect URL is unambiguous.

This is exactly what Scoop's `"checkver": "github"` does, and what `resolve_version()` in `tools/update_manifests.py` does:

```python
final_url, _ = http.request("https://github.com/{owner}/{repo}/releases/latest")
match = re.search(r"/releases/tag/(?P<tag>[^/?#]+)", final_url)
tag = urllib.parse.unquote(match.group("tag"))
```

### REST API route (token recommended)

```text
GET https://api.github.com/repos/{owner}/{repo}/releases/latest
Accept: application/vnd.github+json
Authorization: Bearer <token>        # optional, raises the quota
```

The response is a release object. The version source is `tag_name`; the same object also contains `assets[]` with `browser_download_url` and `digest`.

`/releases/latest` excludes pre-releases and drafts in both routes. If a manifest pins an `api.github.com` URL but no token is available, fall back to the HTML route — that is what `_html_fallback_for_github_api()` does.

> Do not redirect a manifest that asks for a *list* (a JSONPath over `/releases`, for example) to `/releases/latest`. The latest endpoint returns one release and would silently pick the wrong entry.

### From tag to version

Tags and versions are different things:

| Tag | Version | `autoupdate.url` must use |
|---|---|---|
| `v1.2.3` | `1.2.3` | `v$version` |
| `1.2.3` | `1.2.3` | `$version` |
| `release-1.2.3` | `1.2.3` (custom regex) | `release-$version` |
| `app-v1.2.3` | `1.2.3` (custom regex) | `app-v$version` |

The default tag-shaped pattern is:

```text
^(?:v|V)?(?P<version>[\d][\w.+-]*)$
```

The default page-shaped pattern is:

```text
/releases/tag/(?:v|V)?([\d.-]+)
```

If the tag does not carry a plain version, use `checkver.regex` (and `checkver.replace` if needed). Named groups are also exposed to `autoupdate` as `$match<Name>`, which helps when the tag, version and asset name are formatted differently.

## 2. Target a specific tag

Use the tag endpoint:

```text
GET https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}
```

URL-encode the tag as a single path segment (`urllib.parse.quote(tag, safe="")`). This works for stable and pre-release tags alike.

```text
https://api.github.com/repos/what1f/kitter/releases/tags/v0.2.4
```

Related URLs:

| Purpose | URL |
|---|---|
| Human release page | `https://github.com/{owner}/{repo}/releases/tag/{tag}` |
| Asset list fragment (with digests) | `https://github.com/{owner}/{repo}/releases/expanded_assets/{tag}` |
| Asset download | `https://github.com/{owner}/{repo}/releases/download/{tag}/{asset}` |
| Latest asset shortcut | `https://github.com/{owner}/{repo}/releases/latest/download/{asset}` |

`/releases/tags/latest` is not a synonym for `/releases/latest`; it looks for a literal tag named `latest` and normally returns `404`. Use `/releases/latest` when you mean "newest".

If only a tag prefix is known, list releases and match:

```text
GET https://api.github.com/repos/{owner}/{repo}/releases?per_page=100
```

Follow the `Link: <...>; rel="next"` header to paginate. `git ls-remote --tags` can discover tag names without the API, but it provides no assets or digests and lexical sorting is wrong (`v1.10` sorts before `v1.9`), so do not use it to decide "latest".

## 3. Build the download URL

The authoritative value is `assets[].browser_download_url` from the release object. It has the same shape as the constructed URL:

```text
https://github.com/{owner}/{repo}/releases/download/{tag}/{asset_name}
```

Rules:

- Use `assets[].name` verbatim for `{asset_name}`; URL-encode spaces, `+`, `#`, and non-ASCII characters when constructing the URL by hand.
- Match assets by a deliberate architecture token (`windows-x86_64`, `arm64`, `setup`, `portable`), not by array position.
- A `#/Name.exe` suffix is a Scoop fragment for renaming the downloaded file. It is not part of the HTTP request; strip it before comparing URLs.
- Source archives at `/archive/refs/tags/{tag}.zip` are generated by GitHub, are **not** release assets, and have no `digest`.
- `/releases/latest/download/{asset}` is convenient for a stable link when the filename never changes, but it reveals neither the version nor a digest. Use it only when those are not needed.

## 4. Obtain the SHA-256

Try the sources in this order.

### 4.1 Release asset digest (preferred)

Every release asset may carry a `digest` field:

```json
{
    "name": "Kitter-0.2.4-desktop-windows-x86_64.exe",
    "browser_download_url": "https://github.com/what1f/kitter/releases/download/v0.2.4/Kitter-0.2.4-desktop-windows-x86_64.exe",
    "digest": "sha256:d7a2007bba774dfb8c696bcf7059d2738b3f514b41175e616f954543627cdeba"
}
```

The format is `sha256:<64 lowercase hex>`. Strip the prefix; Scoop expects a bare 64-character SHA-256 by default. `format_hash()` in this repo also maps other lengths (`32` → `md5:`, `40` → `sha1:`, `128` → `sha512:`).

The field can be `null` for releases uploaded before GitHub started computing digests. Fall through to the next source in that case.

### 4.2 Public asset-list page (no API quota)

```text
GET https://github.com/{owner}/{repo}/releases/expanded_assets/{tag}
```

This is the fragment GitHub loads when the Assets section is expanded. It lists every asset with its size, upload time, download link and digest:

```html
<a href="/what1f/kitter/releases/download/v0.2.4/Kitter-0.2.4-desktop-windows-x86_64.exe" ...>
...
<span ...>sha256:d7a2007bba774dfb8c696bcf7059d2738b3f514b41175e616f954543627cdeba</span>
```

Parse it by anchoring on the asset link and scanning a bounded window forward for the digest:

```python
pattern = re.compile(
    r'href="[^"]*/releases/download/[^"]*/(?P<file>[^/"]+)"'
    r'(?P<tail>.{0,2000}?)(?P<digest>sha256:[0-9a-f]{64})',
    re.DOTALL,
)
for match in pattern.finditer(page):
    if urllib.parse.unquote(match["file"]) == basename:
        return match["digest"].split(":", 1)[-1]
```

Do not search the whole page once per filename: that can pair one asset's name with a different asset's digest. Compare URL-decoded basenames. Cache the page per tag; it is shared by every asset in that release.

Older releases may omit the digest span entirely. In that case, fall back to `assets[].digest` from the API; if that is also `null`, only a checksum file or a local hash remains.

This page is not part of the REST quota and needs no authentication. It is the route used by `_hash_from_github_digest()` before it calls the API.

### 4.3 Vendor checksum file

If the project publishes `SHA256SUMS`, `checksums.txt`, a JSON manifest, or a per-asset `.sha256` file, use it through `autoupdate.hash`:

```json
"hash": {
    "url": "https://github.com/owner/repo/releases/download/v$version/SHA256SUMS",
    "find": "$sha256\\s+\\*?$basename"
}
```

Text mode supports `$sha256`, `$sha1`, `$md5`, `$sha512`, `$checksum`, `$basename`; JSON mode uses `jsonpath`. A vendor checksum is as trustworthy as the transport that delivered it, and GitHub's own digest is usually simpler.

### 4.4 Hash the downloaded asset (last resort)

Download the asset in chunks and feed `hashlib.sha256`:

```python
digest = hashlib.sha256()
with urllib.request.urlopen(url) as response:
    while chunk := response.read(1 << 20):
        digest.update(chunk)
return digest.hexdigest()
```

Only use this when no digest and no checksum file exist. `tools/update_manifests.py --no-download` turns this step into an error instead.

## 5. Put it into a manifest

```json
{
    "version": "0.2.4",
    "homepage": "https://github.com/what1f/kitter",
    "checkver": "github",
    "autoupdate": {
        "architecture": {
            "64bit": {
                "url": "https://github.com/what1f/kitter/releases/download/v$version/Kitter-$version-desktop-windows-x86_64.exe#/Kitter.exe"
            }
        }
    }
}
```

Key points:

- `version` is the extracted version, **not** the tag.
- The tag is `v0.2.4`, so the template writes `v$version`. Dropping the `v` produces a `404`.
- The static `url` uses the literal version; only `autoupdate.url` uses `$version`. Reversing them breaks installation.
- For a standard GitHub release URL, Scoop's built-in `github` hash mode reads `assets[].digest`, so an explicit `autoupdate.hash` block is optional. This repo's script does the same lookup. Add an explicit `hash.url` when the download is mirrored or the URL only looks like a GitHub release but is served elsewhere, otherwise the automatic mode silently falls back to downloading the asset.

## 6. Pre-releases and nightlies

- `/releases/latest` skips pre-releases and drafts.
- `/releases` returns releases newest-first and includes pre-releases (drafts require authentication).
- To track the newest pre-release, list releases and take the first entry with `"prerelease": true`; one page is often enough, but paginate if the newest entries are all stable.
- A specific pre-release is simplest through `/releases/tags/{tag}`.
- GitHub's HTML release list marks these with a `Pre-release` label. `normalise_github_html()` in this repo injects a `data-prerelease` attribute so a JSONPath-like filter can select them.

## 7. Quotas, authentication and retries

| Route | Anonymous quota | Authenticated quota |
|---|---|---|
| `github.com` HTML (`/releases/latest`, `expanded_assets`) | no REST quota; normal web throttling | n/a |
| `api.github.com` REST | 60 requests/hour per IP | 5,000 requests/hour |

- Send `Authorization: Bearer <token>` **only** to `api.github.com`. Never attach it to `github.com` pages or `objects.githubusercontent.com` downloads.
- Anonymous API exhaustion surfaces as `403` (older behavior) or `429`, with `X-RateLimit-Remaining: 0` and `Retry-After`. Treat it as a quota error, not a missing resource.
- Retry network errors and `5xx` with a short backoff (this repo uses 3 attempts at 1.5 s and 3 s). Do not retry `400`, `401`, `403` (except rate limiting), `404`, `410`, or `422`.
- Set a descriptive `User-Agent` (`Scoop/1.0 (+https://scoop.sh)`).
- Reuse responses within one run: the release object and the asset-list page both cover every asset of a tag.
- The token is optional. For a bucket that only tracks public repos, the HTML route avoids the quota entirely.

## 8. Reference implementation

| Function in `tools/update_manifests.py` | Responsibility |
|---|---|
| `resolve_version()` | `checkver` dispatch; latest redirect; API/HTML selection; regex/JSONPath/script |
| `_version_from_tag()` | Tag → version via `checkver.regex` / `replace` |
| `_html_fallback_for_github_api()` | Translate an `api.github.com` checkver into an equivalent HTML scrape |
| `_hash_from_github_digest()` | `expanded_assets/{tag}` digest first, REST `assets[].digest` fallback |
| `resolve_hash()` | `hash.url` → GitHub digest → local download, in that order |
| `version_variables()` / `substitute()` | `$version`, `$matchX`, `$basename`, … expansion |
| `Http` | UA, retries, token scoping, thread-safe caching and request cap |

Minimal API implementation of the same rules:

```python
import json, re, urllib.parse, urllib.request

UA = {"User-Agent": "Scoop/1.0 (+https://scoop.sh)",
      "Accept": "application/vnd.github+json"}

def release(owner, repo, tag=None, token=None):
    headers = dict(UA)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    path = "latest" if tag is None else f"tags/{urllib.parse.quote(tag, safe='')}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner}/{repo}/releases/{path}",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    return {
        "tag": data["tag_name"],
        "version": re.sub(r"^[vV]", "", data["tag_name"]),
        "assets": {
            asset["name"]: {
                "url": asset["browser_download_url"],
                "sha256": (asset.get("digest") or "").split(":", 1)[-1],
            }
            for asset in data["assets"]
        },
    }
```

## 9. Verify

```pwsh
# Latest tag via the redirect (no API quota)
curl.exe -sSI https://github.com/what1f/kitter/releases/latest |
    Select-String -Pattern '^location:'

# Latest release object
gh api repos/what1f/kitter/releases/latest --jq `
    '.tag_name, (.assets[] | [.name, .digest] | @tsv)'

# A specific tag
gh api repos/what1f/kitter/releases/tags/v0.2.4 --jq '.tag_name'
```

Expected for the first command:

```text
location: https://github.com/what1f/kitter/releases/tag/v0.2.4
```

The digest printed by the second command should equal the `hash` in `bucket/kitter-desktop.json`.

## 10. Pitfall checklist

- [ ] Tag is not a version: strip `v`/`V` only when the tag actually has it.
- [ ] `autoupdate.url` uses `$version`; static `url` uses the literal version.
- [ ] `/releases/latest` excludes pre-releases; use `/releases` or a specific tag for nightlies.
- [ ] `/releases/tags/latest` is not the latest release.
- [ ] `digest` can be `null`; fall back to the asset-list page or a checksum file.
- [ ] Source archives are not release assets and have no digest.
- [ ] Compare URL-decoded asset names, not raw hrefs.
- [ ] Scan a bounded window after each href, not the whole page per asset.
- [ ] Never send the GitHub token to non-API hosts.
- [ ] Do not download an asset when a published digest exists.
