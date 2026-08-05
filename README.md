# pbskids-sync

Nightly pull of PBS Kids **full episodes** into the Jellyfin Kids TV library.

PBS Kids streams are **DRM-free** (`drm_enabled=false`) and the free catalog
**rotates** (assets carry expiration dates). This job discovers the currently
public full episodes of each configured show via PBS Kids' public GraphQL API,
downloads **new** ones at 540p HLS, and publishes them with Jellyfin TV naming.
The ledger keeps everything already downloaded — nothing is pruned, so kids
keep their episodes even after PBS rotates them off the site.

Output:

```
<JELLYFIN_ROOT>/<LIBRARY_SUBDIR>/<Show>/Season NN/<Show> SxxEyy.mkv
```

## How it works

1. Enumerate: `mediaManagerVideoSearch(showSlug, type:"full_length",
   available:"public", page:N, pageSize:50)` — paginate until the backend 404s
   (the stop signal). `available:"public"` = currently downloadable.
2. Diff against the ledger (`/state/<slug>.json`) — only new IDs download.
3. Per video: `pbsKidsVideo(id)` → HLS profile → follow `urs.pbs.org` redirect
   to the CDN master m3u8 → pick the largest rendition ≤ `QUALITY` (default
   540) + the English audio track (skips the audio-description track) →
   `ffmpeg -c copy` remux to `.mkv`.
4. Publish with an atomic same-filesystem rename from the NFS work dir, then
   POST `/Library/Refresh` to Jellyfin if anything changed.

## Config (env)

| Var | Default | Meaning |
|---|---|---|
| `SHOWS` | — | JSON list `[{"slug","show"}, ...]` — PBS slug + Jellyfin show dir |
| `QUALITY` | `540` | target rendition height: 432 \| 540 \| 720 \| 1080 |
| `JELLYFIN_ROOT` | `/jellyfin` | NFS mount of the Jellyfin media root |
| `LIBRARY_SUBDIR` | `Media/Kids_TV` | subdir under JELLYFIN_ROOT |
| `STATE_DIR` | `/state` | ledger dir (one `<slug>.json` per show) |
| `WORK_DIR` | `<JELLYFIN_ROOT>/.pbskids-sync-work` | same-fs staging for atomic rename |
| `GRAPHQL_URL` | `https://graph.services.pbskids.org/graphql` | PBS Kids public API |
| `JELLYFIN_URL` / `JELLYFIN_API_KEY` | — | trigger `/Library/Refresh` after changes |

Example `SHOWS`:

```json
[
  {"slug": "arthur", "show": "Arthur (1996)"},
  {"slug": "curious-george", "show": "Curious George (2006)"},
  {"slug": "daniel-tigers-neighborhood", "show": "Daniel Tiger's Neighborhood (2012)"}
]
```

## Deployment

- Image: `192.168.1.2:3000/reeds/pbskids-sync:latest` (CI builds on push)
- Manifests: `sutliff-cluster/apps/pbskids-sync/` (Flux, ns `pbskids-sync`)
- CronJob: nightly 04:30 America/New_York (same slot as yt-channel-sync)
- Storage: static NFS PV → `/mnt/raid_store/jellyfin` (RWX, same export as
  Jellyfin + yt-sync); state ledger on a Longhorn 1Gi RWO PVC
- Secret `pbskids-sync-secret` (imperative): `JELLYFIN_API_KEY`
- **Jellyfin discovery** relies on `jellyfin-watch` (NFS-local inotify → API
  refresh); the job also fires `/Library/Refresh` itself as belt-and-braces.

## Notes / gotchas

- Podcast entries ("... Podcast: ...") are skipped — audio content, not TV.
- Episodes with no PBS season/episode number land in `Season 00` and get
  auto-numbered from existing files.
- A failed download is left OUT of the ledger → retried next run while the
  episode is still public.
- Enumeration stops on the backend 404 = past the last page (normal).
- First run pulls the whole current public catalog (~110 eps across the 3
  configured shows ≈ 16 GB @ 540p); later runs only grab what rotated in.
