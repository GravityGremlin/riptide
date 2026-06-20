#!/usr/bin/env python
"""riptide - multi-service TIDAL + Qobuz download web UI (no JS)."""

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
LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/app/library"))
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
    }


# ── Qobuz Search (via streamrip CLI) ───────────────────────────
def _job_convert_opus(new_paths: list[Path]) -> list[Path]:
    converted = []
    for src in new_paths:
        if src.suffix.lower() not in {".flac", ".m4a", ".mp3", ".alac", ".wav", ".aiff", ".aif", ".aac"}:
            continue
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
                    "year": "", "track_count": 0,
                })
            for a in (sr.get("albums", []) or [])[:12]:
                arts = getattr(a, "artists", []) or []
                results["albums"].append({
                    "id": a.id, "service": "tidal", "type": "album",
                    "title": a.name,
                    "artist": ", ".join(ar.name for ar in arts),
                    "url": f"https://tidal.com/browse/album/{a.id}",
                    "year": a.release_date.year if getattr(a, "release_date", None) else "",
                    "track_count": a.num_tracks,
                })
            for t in (sr.get("tracks", []) or [])[:12]:
                arts = getattr(t, "artists", []) or []
                track_album = getattr(t, "album", None)
                results["tracks"].append({
                    "id": t.id, "service": "tidal", "type": "track",
                    "title": t.name,
                    "artist": ", ".join(ar.name for ar in arts),
                    "url": f"https://tidal.com/browse/track/{t.id}",
                    "album": track_album.name if track_album else "",
                    "duration": getattr(t, "duration", 0),
                })
        except Exception:
            pass

    # ── Qobuz search ──
    for media_type in ("artist", "album", "track"):
        items = _rip_search("qobuz", media_type, q)
        for item in items:
            target = results.get(f"{media_type}s", results.get(f"{media_type}s", results))
            if media_type == "track":
                results["tracks"].append({
                    **item, "album": "", "duration": 0,
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
    convert_opus = request.form.get("convert_opus") == "1"
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
            "convert_opus": convert_opus,
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
    target = (root / subpath).resolve()
    if not str(target).startswith(str(root.resolve())):
        return render_template("message.html", title="Forbidden", message="Path outside allowed directory.")
    if not target.exists():
        return render_template("message.html", title="Not Found", message="Path not found.")

    items = []
    for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        items.append({
            "name": entry.name,
            "path": str(entry.relative_to(root)) if entry != root else "",
            "is_dir": entry.is_dir(),
            "size": entry.stat().st_size if entry.is_file() else 0,
        })
    parent = ""
    if subpath:
        p = Path(subpath).parent
        parent = str(p) if str(p) != "." else ""
    return render_template(
        "browse.html",
        items=items,
        current=subpath,
        parent=parent,
        label=label,
        route_base=route_base,
        root_dir=str(root),
    )


@app.route("/browse")
@app.route("/browse/<path:subpath>")
def browse(subpath=""):
    return _browse_directory(DOWNLOAD_DIR, subpath, "Downloads", "browse")


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
    try:
        proc = subprocess.Popen(
            cmd_str, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env,
        )

        lines = []
        assert proc.stdout
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            lines.append(line)

            if "Downloading:" in line:
                parts = line.split("Downloading:", 1)
                label = parts[1].strip()[:60] if len(parts) > 1 else ""
                _update(progress=f"Downloading: {label}")
                _update(tracks_done=job.get("tracks_done", 0) + 1)
            elif "%" in line and any(c.isdigit() for c in line[:5]):
                m = re.search(r"(\d+\.?\d*)%", line)
                if m:
                    _update(progress=f"{m.group(1)}%")
            m = _PROGRESS_RE.search(line)
            if m:
                _update(progress=f"Track {m.group(1)} / {m.group(2)}")

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

        if job.get("convert_opus") and new_files:
            _job_convert_opus(new_files)

        if roots:
            _job_run_beets(roots)

        _update(status="completed", finished_at=_now_iso(), log=lines[-20:])
    except Exception as e:
        _update(status="error", finished_at=_now_iso(), log=[str(e)])


# ── Entry Point ────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("FLASK_PORT", 19287))
    app.run(host="0.0.0.0", port=port, debug=False)
