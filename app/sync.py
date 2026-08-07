#!/usr/bin/env python3
"""pbskids-sync — nightly pull of PBS Kids full episodes into the Jellyfin
Kids TV library.

Discovers currently-public full episodes per configured show via PBS Kids'
public GraphQL API (graph.services.pbskids.org, introspection enabled),
downloads new ones via HLS at the configured quality, and publishes them with
Jellyfin TV naming:

    <JELLYFIN_ROOT>/<LIBRARY_SUBDIR>/<Show>/Season NN/<Show> SxxEyy.mkv

No DRM (drm_enabled=false for all PBS Kids assets). The free catalog ROTATES
(assets carry expiration dates) — the ledger records everything already
downloaded so each run only pulls NEW public episodes. Nothing is pruned
(unlike yt-channel-sync: kids rewatch, and the point of downloading is to
KEEP episodes that rotate off the site).

Enumeration API (verified 2026-08-05):
    mediaManagerVideoSearch(showSlug, type:"full_length", available:"public",
                            page, pageSize:50) -> [MediaManagerAsset]
    - paginate until the backend 404s (that's the stop signal)
    - per video: pbsKidsVideo(id) -> mediaManagerAsset.videos[] -> urs.pbs.org
      redirect -> real CDN m3u8 (kids.pbs-video.pbs.org)

Config (env):
    SHOWS            required (JSON list): [{"slug","show"}, ...]
    QUALITY          target height 432|540|720|1080, default 540
    JELLYFIN_ROOT    default /jellyfin   (NFS mount of the Jellyfin media root)
    LIBRARY_SUBDIR   default Media/Kids_TV
    STATE_DIR        default /state       (one <slug>.json per show)
    WORK_DIR         default <JELLYFIN_ROOT>/.pbskids-sync-work (same-fs rename)
    GRAPHQL_URL      default https://graph.services.pbskids.org/graphql
    JELLYFIN_URL     optional; POST /Library/Refresh after changes
    JELLYFIN_API_KEY optional
"""

import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SHOWS_RAW = os.environ.get("SHOWS", "").strip()
QUALITY = int(os.environ.get("QUALITY", "540"))
JELLYFIN_ROOT = Path(os.environ.get("JELLYFIN_ROOT", "/jellyfin"))
LIBRARY_SUBDIR = os.environ.get("LIBRARY_SUBDIR", "Media/Kids_TV").strip().strip("/")
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
GRAPHQL_URL = os.environ.get("GRAPHQL_URL", "https://graph.services.pbskids.org/graphql")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "").strip()
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "").strip()
# Work dir MUST live on the same filesystem as the library (NFS) so the final
# publish is an atomic rename — an emptyDir work dir would cross devices (EXDEV).
WORK_DIR = Path(os.environ.get("WORK_DIR") or (JELLYFIN_ROOT / ".pbskids-sync-work"))

PAGE_SIZE = 50
INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*]')
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"


# ---------------------------------------------------------------------------
# IPv4-forced HTTP (this host's DNS64 returns NAT64 ::ffff/64:ff9b addresses;
# Python's urllib tries IPv6 first and hangs. curl -4 is the equivalent.)
# ---------------------------------------------------------------------------

class _V4HTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        self.sock = socket.create_connection(
            (socket.gethostbyname(self.host), self.port), timeout=30)
        self.sock.settimeout(30)
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _V4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_V4HTTPSConnection, req)


_OPENER = urllib.request.build_opener(_V4HTTPSHandler)


def http_open(url: str, timeout: int = 30, data: bytes | None = None,
              headers: dict | None = None) -> object:
    """urllib open forced to IPv4. Accepts a URL or a Request."""
    req = url if isinstance(url, urllib.request.Request) else urllib.request.Request(
        url, data=data, headers=headers or {"User-Agent": UA})
    return _OPENER.open(req, timeout=timeout)


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def fail(msg: str) -> None:
    log(f"FATAL: {msg}")
    sys.exit(1)


def load_shows() -> list[dict]:
    if not SHOWS_RAW:
        fail("set SHOWS (JSON list of {\"slug\", \"show\"})")
    try:
        shows = json.loads(SHOWS_RAW)
    except json.JSONDecodeError as exc:
        fail(f"SHOWS is not valid JSON: {exc}")
    if not isinstance(shows, list) or not shows:
        fail("SHOWS must be a non-empty JSON list")
    out = []
    for s in shows:
        if not s.get("slug") or not s.get("show"):
            fail(f"each show needs slug+show: {s}")
        out.append({"slug": str(s["slug"]), "show": str(s["show"])})
    return out


def slug(show: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", show.lower()).strip("-") or "show"


# ---------------------------------------------------------------------------
# PBS Kids GraphQL
# ---------------------------------------------------------------------------

def gql(query: str, max_retries: int = 3) -> dict:
    """POST one GraphQL query. Returns the full JSON body (data or errors)."""
    body = json.dumps({"query": query}).encode()
    last = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = http_open(GRAPHQL_URL, timeout=30, data=body,
                             headers={"Content-Type": "application/json",
                                      "Origin": "https://pbskids.org",
                                      "User-Agent": UA})
            return json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            log(f"  graphql attempt {attempt}/{max_retries} failed: {exc}")
            if attempt < max_retries:
                time.sleep(5 * attempt)
    raise RuntimeError(f"graphql failed after {max_retries} tries: {last}")


def list_public_episodes(slug_: str) -> list[dict]:
    """All currently-public full episodes of a show (paginated)."""
    out = []
    page = 1
    while page <= 20:  # hard cap: 20*50 = 1000 eps, more than any show
        q = ('{ mediaManagerVideoSearch(showSlug: "%s", type: "full_length",'
             ' available: "public", page: %d, pageSize: %d)'
             '{ id legacy_tp_media_id slug title season_number episode_number } }'
             % (slug_, page, PAGE_SIZE))
        data = gql(q)
        if "errors" in data:
            # 404 when past the last page = normal stop signal
            err = str(data["errors"][0].get("message", ""))[:60]
            if "404" in err or "Not Found" in err:
                break
            raise RuntimeError(f"search errors: {err}")
        vids = data.get("data", {}).get("mediaManagerVideoSearch") or []
        if not vids:
            break
        out.extend(vids)
        if len(vids) < PAGE_SIZE:
            break
        page += 1
    return out


def get_video(tp_media_id: str) -> dict:
    """Full PbsKidsVideo for one asset via its legacy TP media id.

    The search API returns MediaManagerAsset ids (GUIDs) but pbsKidsVideo(id:)
    expects the numeric CMS id and 500s on GUIDs — getVideoByTpMediaId is the
    reliable bridge (verified 2026-08-05).
    """
    q = ('{ getVideoByTpMediaId(tpMediaId: "%s") { id guid slug title '
         ' mediaManagerAsset { drm_enabled season_number episode_number '
         ' duration description_short videos { profile url } } } }' % tp_media_id)
    data = gql(q)
    if "errors" in data:
        raise RuntimeError(f"video {tp_media_id} errors: {data['errors'][0]['message'][:120]}")
    return data["data"]["getVideoByTpMediaId"]


def master_url_from_video(vid: dict) -> str:
    """Follow the first hls profile redirect to the real CDN master m3u8."""
    asset = (vid.get("mediaManagerAsset") or {})
    if asset.get("drm_enabled"):
        raise RuntimeError("video is DRM-enabled — cannot download")
    vids = asset.get("videos") or []
    hls = next((v for v in vids
                if (v.get("profile") or "").startswith("hls-")
                and "drm" not in (v.get("profile") or "").lower()), None)
    if not hls:
        raise RuntimeError("no non-DRM HLS profile in asset")
    req = urllib.request.Request(hls["url"], headers={"User-Agent": UA})
    resp = http_open(req, timeout=30)
    return resp.geturl()  # final URL after urs.pbs.org 302 chain


# ---------------------------------------------------------------------------
# HLS resolution + download
# ---------------------------------------------------------------------------

def fetch_text(url: str) -> str:
    resp = http_open(url, timeout=30)
    return resp.read().decode("utf-8", "replace")


def resolve_url(base: str, uri: str) -> str:
    return urllib.parse.urljoin(base, uri)


def pick_variant(master_url: str, target: int) -> tuple[str, str | None]:
    """Choose (video_variant_url, english_audio_url) from a master playlist.

    Selects the largest rendition whose height <= target (540 by default);
    audio = the English EXT-X-MEDIA group (skips the AD/audio-description
    track). Returns audio None when the master has no separate audio group.
    """
    text = fetch_text(master_url)
    variants = []  # (height, uri)
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            if m and i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                variants.append((int(m.group(2)), lines[i + 1].strip()))
    if not variants:
        # single-rendition playlist — use it as-is
        return master_url, None
    variants.sort(key=lambda v: v[0])
    chosen = variants[0]
    for h, uri in variants:
        if h <= target:
            chosen = (h, uri)
        else:
            break
    log(f"  variant: {chosen[0]}p ({len(variants)} renditions, target {target}p)")
    video_url = resolve_url(master_url, chosen[1])

    audio_url = None
    for line in lines:
        if line.startswith("#EXT-X-MEDIA") and 'TYPE=AUDIO' in line:
            name_m = re.search(r'NAME="([^"]+)"', line)
            uri_m = re.search(r'URI="([^"]+)"', line)
            # prefer plain "English", skip "English AD" (audio description)
            if name_m and uri_m and "AD" not in name_m.group(1):
                audio_url = resolve_url(master_url, uri_m.group(1))
                break
    return video_url, audio_url


def download_episode(video_id: str, master_url: str, work: Path) -> Path:
    """ffmpeg-remux the chosen HLS rendition (+ English audio) to mkv."""
    video_url, audio_url = pick_variant(master_url, QUALITY)
    out = work / f"{video_id}.mkv"
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-i", video_url]
    if audio_url:
        cmd += ["-i", audio_url]
    cmd += ["-map", "0:v:0"]
    if audio_url:
        cmd += ["-map", "1:a:0"]
    cmd += ["-c", "copy", "-f", "matroska", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-6:])
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {tail}")
    if not out.exists() or out.stat().st_size < 1_000_000:
        raise RuntimeError("ffmpeg reported success but no valid mkv produced")
    chk = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(out)],
        capture_output=True, text=True)
    if chk.returncode != 0 or not chk.stdout.strip():
        out.unlink(missing_ok=True)
        raise RuntimeError(f"ffprobe rejected download of {video_id}: {chk.stderr[:300]}")
    log(f"  downloaded {out.stat().st_size // (1024 * 1024)} MB, "
        f"{float(chk.stdout.strip()):.0f}s")
    return out


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001
            log(f"state {path.name} corrupt ({exc}) — starting fresh")
    return {}


def write_provenance_sidecar(video: Path, prov: dict) -> None:
    """Write/merge the provenance block into <video>.finalized.json (or create
    the sidecar if finalize hasn't run yet). Never overwrites checks/actions —
    the finalize crawl owns those; this only sets provenance + flags."""
    sidecar = video.with_suffix(video.suffix + ".finalized.json")
    m = {}
    if sidecar.exists():
        try:
            m = json.loads(sidecar.read_text())
        except Exception:
            m = {}
    m.setdefault("schema_version", 2)
    m.setdefault("file", str(video.relative_to(JELLYFIN_ROOT)))
    m["provenance"] = prov
    sidecar.write_text(json.dumps(m, indent=2))


def save_state(path: Path, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Per-show sync
# ---------------------------------------------------------------------------

def next_free_number(season_dir: Path) -> int:
    """Next episode number for episodes with no PBS episode_number."""
    highest = 0
    if season_dir.exists():
        for f in season_dir.iterdir():
            m = re.search(r"S\d{2}E(\d+)", f.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def sync_show(cfg: dict, changed_ref: list) -> None:
    show = cfg["show"]
    log(f"==== {show} (slug {cfg['slug']}) ====")
    state_path = STATE_DIR / f"{slug(show)}.json"
    state = load_state(state_path)

    try:
        episodes = list_public_episodes(cfg["slug"])
    except Exception as exc:  # noqa: BLE001 - one show must not kill the run
        log(f"!! {show}: enumeration failed: {exc}")
        return
    log(f"{show}: {len(episodes)} public full episodes")

    # skip audio-only content (e.g. "Arthur Podcast: ...")
    episodes = [e for e in episodes if "podcast" not in (e.get("title") or "").lower()]
    log(f"{show}: {len(episodes)} after podcast filter, "
        f"{len(state)} already in ledger")

    for e in episodes:
        tp_id = str(e.get("legacy_tp_media_id") or "")
        vid = tp_id or str(e["id"])
        rec = state.get(vid)
        if rec:
            if (JELLYFIN_ROOT / rec.get("file", "")).exists():
                continue  # already published and present
            log(f"  ledger entry missing file — re-downloading {e['title']!r}")

        # authoritative metadata comes from the asset (season/ep override)
        try:
            video_info = get_video(tp_id) if tp_id else {"mediaManagerAsset": {}}
            asset = video_info.get("mediaManagerAsset") or {}
        except Exception as exc:  # noqa: BLE001
            log(f"  !! {e['title']!r}: metadata fetch failed: {exc}")
            continue
        season = int(asset.get("season_number") or e.get("season_number") or 0)
        ep_num = int(asset.get("episode_number") or e.get("episode_number") or 0)
        season_dir = JELLYFIN_ROOT / LIBRARY_SUBDIR / show / f"Season {season:02d}"
        if ep_num <= 0:
            ep_num = next_free_number(season_dir)
        fname = f"{show} S{season:02d}E{ep_num:02d}.mkv"
        dest = season_dir / fname
        if dest.exists():
            # already on disk even if ledger missed it — record and move on
            state[vid] = {"file": str(dest.relative_to(JELLYFIN_ROOT)),
                          "title": e.get("title", ""), "season": season,
                          "ep": ep_num, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            continue

        log(f"  downloading {fname}: {e.get('title')!r}")
        work = WORK_DIR / vid
        work.mkdir(parents=True, exist_ok=True)
        try:
            master = master_url_from_video(video_info)
            video = download_episode(vid, master, work)
        except Exception as exc:  # noqa: BLE001 - retry next run while public
            log(f"  !! {fname} failed (will retry while public): {exc}")
            shutil.rmtree(work, ignore_errors=True)
            continue

        season_dir.mkdir(parents=True, exist_ok=True)
        try:
            video.rename(dest)
            os.chmod(dest, 0o644)
        except OSError as exc:
            # EXDEV safety net — copy across filesystems, then clean work
            shutil.copy2(video, dest)
            log(f"  cross-device publish via copy: {exc}")
        shutil.rmtree(work, ignore_errors=True)
        state[vid] = {"file": str(dest.relative_to(JELLYFIN_ROOT)),
                      "title": e.get("title", ""), "season": season,
                      "ep": ep_num, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        # Provenance sidecar (media-finalization contract, Reed 2026-08-06):
        # PBS sets trusted_source=true and SEARCHES tvdb_id (the finalize crawl
        # fills tvdb_id — the dispatcher has the TVDB key; this pod doesn't).
        write_provenance_sidecar(dest, {
            "trusted_source": True,
            "tvdb_id": None,  # backfilled by finalize's pbs branch
            "downloader": "pbskids-sync",
            "source_urls": [f"https://pbskids.org/video/watch/{vid}"],
            "video_id": vid,
            "pbs_show": show, "pbs_season": season, "pbs_episode": ep_num,
            "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        log(f"  -> {dest.relative_to(JELLYFIN_ROOT)}")
        changed_ref[0] = True

    save_state(state_path, state)
    log(f"{show}: {len(state)} episodes in ledger")


def jellyfin_refresh() -> None:
    if not (JELLYFIN_URL and JELLYFIN_API_KEY):
        log("no Jellyfin API key configured — relying on jellyfin-watch")
        return
    url = f"{JELLYFIN_URL.rstrip('/')}/Library/Refresh"
    try:
        resp = http_open(url, timeout=30, headers={"X-Emby-Token": JELLYFIN_API_KEY},
                         data=b"")
        log(f"Jellyfin /Library/Refresh -> HTTP {getattr(resp, 'status', '?')}")
    except Exception as exc:  # noqa: BLE001
        log(f"Jellyfin refresh failed: {exc}")


def main() -> None:
    shows = load_shows()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    # clear any stale partial downloads (e.g. from a killed pod)
    for d in WORK_DIR.iterdir():
        shutil.rmtree(d, ignore_errors=True)

    changed = [False]
    for cfg in shows:
        try:
            sync_show(cfg, changed)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad show must not kill the run
            log(f"!! show {cfg['show']} failed: {exc}")

    log("sync complete")
    if changed[0]:
        jellyfin_refresh()
    else:
        log("nothing to do")


if __name__ == "__main__":
    main()



