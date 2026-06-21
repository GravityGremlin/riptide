#!/usr/bin/env python
"""riptide - multi-service TIDAL + Qobuz download web UI (no JS)."""

__version__ = "0.3.0"

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import tidalapi
from flask import Flask, flash, redirect, render_template, request, url_for

# ── Config ─────────────────────────────────────────────────────
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/app/downloads"))
LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/music"))
TIDAL_CONFIG_DIR = Path(os.environ.get("TIDAL_CONFIG_DIR", "~/.config/tidal_dl_ng")).expanduser()
STREAMRIP_CONFIG_DIR = Path(os.environ.get("STREAMRIP_CONFIG_DIR", "~/.config/streamrip")).expanduser()
JOBS_DIR = Path("/app/data")
JOBS_FILE = JOBS_DIR / "jobs.json"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "riptide-change-me")

# Ensure dirs exist
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)
TIDAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
STREAMRIP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# ── Job State ─────────────────────────────────────────────────
jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _load_jobs():
    if JOBS_FILE.exists():
        with open(JOBS_FILE) as f:
            jobs.clear()
            jobs.update(json.load(f))


def _save_jobs():
    with open(JOBS_FILE, "w") as f:
        json.dump(jobs, f, indent=2, default=str)


_load_jobs()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── Service Detection ──────────────────────────────────────────
SERVICES = {
    "tidal": {
        "label": "Tidal",
        "color": "#00ffff",
        "url_pattern": "tidal.com/browse",
        "download_cmd": "tidal-dl-ng dl {url}",
    },
    "qobuz": {
        "label": "Qobuz",
        "color": "#00bfff",
        "url_pattern": "open.qobuz.com",
        "download_cmd": "rip url {url}",
    },
}

DOWNLOAD_COMMANDS = {
    "tidal.com": lambda url: f"tidal-dl-ng dl {url}",
    "open.qobuz.com": lambda url: f"rip url {url}",
    "qobuz.com": lambda url: f"rip url {url}",
}


def detect_service(url: str) -> str:
    for key, fn in DOWNLOAD_COMMANDS.items():
        if key in url:
            return fn(url)
    return ""


# ── Tidal Session ──────────────────────────────────────────────
def get_tidal_session() -> tidalapi.Session | None:
    token_file = TIDAL_CONFIG_DIR / "token.json"
    if not token_file.exists():
        return None
    try:
        token_data = json.loads(token_file.read_text())
        expiry = datetime.fromtimestamp(token_data["expiry_time"])
        session = tidalapi.Session()
        session.load_oauth_session(
            token_type=token_data["token_type"],
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            expiry_time=expiry,
        )
        return session
    except Exception:
        return None


@app.context_processor
def _inject_globals():
    return {
        "tidal_ok": get_tidal_session() is not None,
        "download_dir": str(DOWNLOAD_DIR),
        "version": __version__,
    }


# ── Qobuz Search (via streamrip CLI) ───────────────────────────
def _job_convert_opus(new_paths: list[Path], job_id: str = None) -> list[Path]:
    def update_prog(msg):
        if not job_id: return
        with _jobs_lock:
            j = jobs.get(job_id)
            if j: j["progress"] = msg
            _save_jobs()

    converted = []
    targets = [p for p in new_paths if p.suffix.lower() in {".flac", ".m4a", ".mp3", ".alac", ".wav", ".aiff", ".aif", ".aac"}]
    total = len(targets)
    
    for i, src in enumerate(targets, 1):
        update_prog(f"Converting Opus: {i}/{total} ({src.name})")
        dst = src.with_suffix(".opus")
        tmp_dst = src.with_name(src.name + ".tmp.opus")
        proc = subprocess.run([
            "ffmpeg", "-y", "-i", str(src), "-c:a", "libopus", "-b:a", "160k", str(tmp_dst)
        ], capture_output=True, text=True)
        if proc.returncode != 0 or not tmp_dst.exists():
            raise RuntimeError(f"ffmpeg conversion failed for {src.name}")
        tmp_dst.replace(dst)
        src.unlink(missing_ok=True)
        converted.append(dst)
    return converted


def _job_run_beets(roots: list[Path]) -> None:
    config = Path("/app/.config/beets/config.yaml")
    if not shutil.which("beet") or not config.exists():
        return
    for root in roots:
        if not root.exists():
            continue
        try:
            subprocess.run([
                "beet", "-c", str(config), "import", "-q", str(root)
            ], capture_output=True, text=True, timeout=300)
        except Exception:
            # Beets import failed - log but don't fail the job
            pass


def _rip_search(source: str, media_type: str, query: str) -> list[dict]:
    """Run `rip search <source> <type> <query> -o <tmpfile>` and parse JSON."""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
    tmp.close()
    try:
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(STREAMRIP_CONFIG_DIR.parent)

        result = subprocess.run(
            ["rip", "search", source, media_type, query, "-o", tmp.name, "-n", "8"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.returncode != 0:
            return []

        with open(tmp.name) as f:
            data = json.load(f)

        items = []
        for item in data:
            item_type = item.get("media_type", "")
            desc = item.get("desc", "")
            item_id = item.get("id", "")

            title = desc
            artist = ""
            if " by " in desc:
                parts = desc.rsplit(" by ", 1)
                title = parts[0].strip()
                artist = parts[1].strip()
            elif " - " in desc:
                parts = desc.rsplit(" - ", 1)
                title = parts[0].strip()
                artist = parts[1].strip()

            url_map = {
                "album": f"https://open.qobuz.com/album/{item_id}",
                "artist": f"https://open.qobuz.com/artist/{item_id}",
                "track": f"https://open.qobuz.com/track/{item_id}",
            }
            url = url_map.get(item_type, "")

            items.append({
                "id": item_id,
                "service": source,
                "type": item_type,
                "title": title,
                "artist": artist,
                "url": url,
                "year": "",
                "track_count": 0,
            })
        return items
    except Exception:
        return []
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
# ── Routes ─────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return redirect(url_for("index"))

    results = {"artists": [], "albums": [], "tracks": []}

    # ── Tidal search ──
    session = get_tidal_session()
    if session:
        try:
            sr = session.search(q)
            for a in (sr.get("artists", []) or [])[:12]:
                results["artists"].append({
                    "id": a.id, "service": "tidal", "type": "artist",
                    "title": a.name, "artist": "",
                    "url": f"https://tidal.com/browse/artist/{a.id}",
                    "year": "", "track_count": 0, "cover": a.image(320) if hasattr(a, "image") else ""
                })
            for a in (sr.get("albums", []) or [])[:12]:
                arts = getattr(a, "artists", [])
                results["albums"].append({
                    "id": a.id, "service": "tidal", "type": "album",
                    "title": a.name,
                    "artist": ", ".join(ar.name for ar in arts),
                    "url": f"https://tidal.com/browse/album/{a.id}",
                    "year": a.release_date.year if getattr(a, "release_date", None) else "",
                    "track_count": a.num_tracks,
                    "cover": a.image(320) if hasattr(a, "image") else ""
                })
            for t in (sr.get("tracks", []) or [])[:12]:
                arts = getattr(t, "artists", [])
                track_album = getattr(t, "album", None)
                results["tracks"].append({
                    "id": t.id, "service": "tidal", "type": "track",
                    "title": t.name,
                    "artist": ", ".join(ar.name for ar in arts),
                    "url": f"https://tidal.com/browse/track/{t.id}",
                    "album": track_album.name if track_album else "",
                    "duration": getattr(t, "duration", 0),
                    "cover": track_album.image(320) if track_album and hasattr(track_album, "image") else ""
                })
        except Exception:
            pass

    # ── Qobuz search ──
        for media_type in ("artist", "album", "track"):
            items = _rip_search("qobuz", media_type, q)
            for item in items:
                item["cover"] = ""  # No cover art available via streamrip CLI
                target = results.get(f"{media_type}s")
                if media_type == "track":
                    results["tracks"].append({
                        **item, "album": "", "duration": 0
                    })
                else:
                    target.append(item)

    return render_template("results.html", q=q, results=results)


@app.route("/artist/<artist_id>")
def artist_albums(artist_id: str):
    service = request.args.get("service", "tidal")
    q = request.args.get("q", "")

    if service == "tidal":
        session = get_tidal_session()
        if not session:
            return render_template("message.html", title="Error", message="No Tidal session.")
        try:
            artist_obj = session.artist(int(artist_id))
            albums = artist_obj.get_albums()
        except Exception as e:
            return render_template("message.html", title="Error", message=str(e))

        album_list = []
        for a in (albums or []):
            album_list.append({
                "id": a.id, "service": "tidal",
                "title": a.name,
                "year": a.release_date.year if getattr(a, "release_date", None) else "",
                "track_count": a.num_tracks,
                "url": f"https://tidal.com/browse/album/{a.id}",
                "cover": a.image(320) if hasattr(a, "image") else ""
            })
        return render_template("albums.html", artist_name=artist_obj.name, albums=album_list, q=q, service="tidal")

    if service == "qobuz":
        return render_template("qobuz_artist.html", artist_id=artist_id, q=q)

    return render_template("message.html", title="Unknown service", message=f"Unknown service: {service}")


@app.route("/album/<album_id>")
def album_tracks(album_id: str):
    service = request.args.get("service", "tidal")

    if service == "tidal":
        session = get_tidal_session()
        if not session:
            return render_template("message.html", title="Error", message="No Tidal session.")
        try:
            album = session.album(int(album_id))
            tracks = album.tracks()
        except Exception as e:
            return render_template("message.html", title="Error", message=str(e))

        track_list = []
        for t in (tracks or []):
            arts = getattr(t, "artists", []) or []
            track_list.append({
                "id": t.id,
                "num": t.track_num if hasattr(t, "track_num") else 0,
                "title": t.name,
                "artist": ", ".join(ar.name for ar in arts),
                "url": f"https://tidal.com/browse/track/{t.id}",
                "duration": getattr(t, "duration", 0),
            })
        artist_name = ", ".join(ar.name for ar in (getattr(album, "artists", []) or []))
        return render_template(
            "tracks.html",
            album_id=album_id,
            album_title=album.name,
            artist_name=artist_name,
            tracks=track_list,
            download_url=f"https://tidal.com/browse/album/{album_id}",
            service="tidal",
        )

    elif service == "qobuz":
        # Show Qobuz tracks - we can't list them via CLI but can download the album directly
        return render_template(
            "tracks.html",
            album_id=album_id,
            album_title="Qobuz Album",
            artist_name="",
            tracks=[],
            download_url=f"https://open.qobuz.com/album/{album_id}",
            service="qobuz",
        )

    return render_template("message.html", title="Unknown service", message=f"Unknown service: {service}")


@app.route("/download", methods=["POST"])
def download():
    urls = request.form.getlist("url")
    if not urls:
        flash("No items selected.")
        return redirect(request.referrer or url_for("index"))

    job_ids = []
    for url in urls:
        cmd = detect_service(url)
        if not cmd:
            continue

        job_id = str(uuid.uuid4())[:8]
        job = {
            "id": job_id,
            "url": url,
            "service": "tidal" if "tidal.com" in url else "qobuz",
            "type": "album" if "/album/" in url else "track",
            "status": "queued",
            "progress": "",
            "tracks_done": 0,
            "tracks_total": 0,
            "command": cmd,
            "log": [],
            "created_at": _now_iso(),
            "started_at": None,
            "finished_at": None,
        }
        with _jobs_lock:
            jobs[job_id] = job
        _save_jobs()
        job_ids.append(job_id)
        threading.Thread(target=_run_download, args=(job_id,), daemon=True).start()

    if len(job_ids) == 1:
        return redirect(url_for("job_status", job_id=job_ids[0]))
    return redirect(url_for("jobs_list"))


@app.route("/status/<job_id>")
def job_status(job_id: str):
    with _jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return render_template("message.html", title="Not Found", message=f"Job {job_id} not found.")
    return render_template("status.html", job=job)


@app.route("/jobs")
def jobs_list():
    with _jobs_lock:
        job_list = list(jobs.values())
    job_list.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return render_template("jobs.html", jobs=job_list)


def _browse_directory(root: Path, subpath: str, label: str, route_base: str):
    import os
    import mimetypes
    from flask import Response
    
    root_str = str(root)
    target_str = os.path.join(root_str, subpath) if subpath else root_str
    
    # Security: check resolved path stays within root
    if not os.path.realpath(target_str).startswith(os.path.realpath(root_str)):
        return render_template("message.html", title="Forbidden", message="Path outside allowed directory.")
    
    # NFS attribute caching: os.path.exists/isfile unreliable
    # Strategy: try listdir (directory), then open (file), then 404
    
    # 1. Try as directory
    try:
        entries = os.listdir(target_str)
    except (NotADirectoryError, FileNotFoundError):
        entries = None
    except PermissionError:
        return render_template("message.html", title="Forbidden", message="Permission denied.")
    
    if entries is not None:
        items = []
        for name in entries:
            if name == "library.db":
                continue
            full_path = os.path.join(target_str, name)
            is_dir = os.path.isdir(full_path)
            
            cover_url = ""
            if is_dir:
                cover_path = os.path.join(full_path, "cover.jpg")
                if os.path.exists(cover_path):
                    cover_url = f"/{route_base}/{os.path.relpath(full_path, root_str)}/cover.jpg"
            
            items.append({
                "name": name,
                "is_dir": is_dir,
                "path": os.path.relpath(full_path, root_str) if full_path != root_str else "",
                "size": os.path.getsize(full_path) if not is_dir else 0,
                "cover": cover_url
            })
        
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        
    parent = ""
    breadcrumbs = []
    if subpath:
        parent = os.path.dirname(subpath)
        if parent == ".":
            parent = ""
            
        parts = subpath.split(os.sep)
        cum_path = ""
        for p in parts:
            cum_path = os.path.join(cum_path, p) if cum_path else p
            breadcrumbs.append({"name": p, "path": cum_path})

    return render_template(
        "browse.html", items=items, current=subpath, parent=parent, breadcrumbs=breadcrumbs,
        label=label, route_base=route_base, root_dir=root_str
    )
    
    # 2. Try as file (NFS: open works even when exists() returns False)
    if target_str.endswith("library.db"):
        return render_template("message.html", title="Forbidden", message="Permission denied.")
    
    try:
        fh = open(target_str, "rb")
    except (FileNotFoundError, IsADirectoryError):
        return render_template("message.html", title="Not Found", message="Path not found.")
    
    def generate():
        with fh:
            while True:
                chunk = fh.read(8192)
                if not chunk:
                    break
                yield chunk
    
    mime = mimetypes.guess_type(target_str)[0] or "application/octet-stream"
    return Response(generate(), mimetype=mime)


@app.route("/browse")
@app.route("/browse/<path:subpath>")
def browse(subpath=""):
    return redirect(url_for("library", subpath=subpath))


@app.route("/library")
@app.route("/library/<path:subpath>")
def library(subpath=""):
    return _browse_directory(LIBRARY_DIR, subpath, "Library", "library")


# ── Background Download Runner ─────────────────────────────────
_PROGRESS_RE = re.compile(r"(\d+)/(\d+)")


def _run_download(job_id: str):
    with _jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return

    def _update(**kw):
        with _jobs_lock:
            j = jobs.get(job_id)
            if j:
                j.update(kw)
                _save_jobs()

    _update(status="running", started_at=_now_iso())

    env = os.environ.copy()
    env["HOME"] = str(Path.home())
    env["XDG_CONFIG_HOME"] = str(STREAMRIP_CONFIG_DIR.parent)

    before_files = {p.resolve() for p in DOWNLOAD_DIR.rglob("*") if p.is_file()}
    before_dirs = {p.resolve() for p in DOWNLOAD_DIR.rglob("*") if p.is_dir()}
    cmd_str = job["command"]
    import re
    progress_re = re.compile(r"(\d+)%")

    try:
        proc = subprocess.Popen(
            cmd_str, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, bufsize=1
        )

        lines = []
        assert proc.stdout

        buffer = ""
        while True:
            char = proc.stdout.read(1)
            if not char:
                break
            if char in ('\r', '\n'):
                if buffer:
                    line = buffer.strip()
                    lines.append(line)
                    if len(lines) > 50:
                        lines.pop(0)

                    m = progress_re.search(line)
                    if m:
                        pct = m.group(1)
                        if "Item" in line:
                            label = line.split("Item", 1)[1].split("\u2501")[0].strip(" '")
                            _update(progress=f"DL: {label[:20]}... {pct}%")
                        elif "List" in line:
                            label = line.split("List", 1)[1].split("\u2501")[0].strip(" '")
                            _update(progress=f"DL Album: {label[:20]}... {pct}%")
                        elif "Downloading" in line:
                            label = line.split("Downloading", 1)[1].split()[0]
                            _update(progress=f"DL: {label[:20]}... {pct}%")
                        else:
                            _update(progress=f"Downloading... {pct}%")
                    else:
                        if "Downloading" in line and "%" not in line:
                            parts = line.split("Downloading", 1)
                            if len(parts) > 1:
                                _update(progress=f"DL: {parts[1].strip()[:30]}")
                buffer = ""
            else:
                buffer += char

        proc.wait()
        rc = proc.returncode
        if rc != 0:
            _update(status="error", finished_at=_now_iso(), log=lines[-20:])
            return

        after_files = {p.resolve() for p in DOWNLOAD_DIR.rglob("*") if p.is_file()}
        new_files = sorted(after_files - before_files)
        new_dirs = sorted({p.resolve() for p in DOWNLOAD_DIR.rglob("*") if p.is_dir()} - before_dirs)
        roots = sorted({p.parent if p.is_file() else p for p in new_files}, key=lambda p: str(p))
        if not roots:
            roots = new_dirs
        _update(status="processing", progress="Processing downloads")

        if new_files:
            _job_convert_opus(new_files, job_id)

        if roots:
            _update(progress="Importing into Beets library")
            _job_run_beets(roots)

        _update(status="completed", finished_at=_now_iso(), log=lines[-20:])
    except Exception as e:
        _update(status="error", finished_at=_now_iso(), log=[str(e)])


# ── Entry Point ────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("FLASK_PORT", 19287))
    app.run(host="0.0.0.0", port=port, debug=False)
