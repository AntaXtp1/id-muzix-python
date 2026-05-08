from flask import Flask, jsonify, request
from flask_cors import CORS
from ytmusicapi import YTMusic
import os
import re
import time
import threading

app = Flask(__name__)
CORS(app)

# ─── Init YTMusic ─────────────────────────────────────────────────────────────
ytmusic = YTMusic()

# ─── Simple in-memory cache ───────────────────────────────────────────────────
_cache = {}
_cache_lock = threading.Lock()

def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        if time.time() > item["expires"]:
            del _cache[key]
            return None
        return item["data"]

def cache_set(key, data, ttl=1800):
    with _cache_lock:
        _cache[key] = {"data": data, "expires": time.time() + ttl}

# ─── Helper: clean_thumbnail ──────────────────────────────────────────────────
# PORT dari main.py project lama — lebih robust dari versi sebelumnya:
#   1. Sort by width*height, bukan ambil index [-1]
#   2. Handle 2 format CDN: lh3.googleusercontent.com dan i.ytimg.com
#   3. Upgrade resolusi otomatis via regex
#   4. Hard fallback ke ytimg URL kalau ada videoId
def clean_thumbnail(thumbnails: list, video_id: str = "") -> str:
    if not thumbnails:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""

    sorted_thumbs = sorted(
        [t for t in thumbnails if isinstance(t, dict)],
        key=lambda x: x.get("width", 0) * x.get("height", 0),
        reverse=True
    )
    if not sorted_thumbs:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""

    url = sorted_thumbs[0].get("url", "")
    if not url:
        return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""

    # lh3.googleusercontent.com: =w226-h226-l90-rj → =w500-h500-l90-rj
    if "lh3.googleusercontent.com" in url:
        return re.sub(r"=w\d+-h\d+", "=w500-h500", url)

    # i.ytimg.com: hqdefault → maxresdefault
    if "i.ytimg.com" in url:
        return re.sub(r"/(hqdefault|mqdefault|sddefault|default)(\.jpg)", r"/maxresdefault\2", url)

    return re.sub(r"=w\d+-h\d+", "=w500-h500", url)


# ─── Helper: parse satu track ytmusicapi → dict bersih ───────────────────────
# PORT dari ytm_track_to_dict di main.py — termasuk album thumbnail fallback
def ytm_track_to_dict(track: dict) -> dict:
    try:
        thumbnails = track.get("thumbnails") or []
        if not thumbnails and track.get("album"):
            thumbnails = track["album"].get("thumbnails") or []

        video_id    = track.get("videoId", "")
        artists     = track.get("artists") or []
        artist_name = ", ".join(
            [a.get("name", "") for a in artists if isinstance(a, dict)]
        ) if artists else ""

        album_obj = track.get("album") or {}
        album     = album_obj.get("name", "") if isinstance(album_obj, dict) else ""

        return {
            "rank":      0,
            "title":     track.get("title", ""),
            "artist":    artist_name,
            "album":     album,
            "thumbnail": clean_thumbnail(thumbnails, video_id),
            "videoId":   video_id,
            "query":     f"{track.get('title', '')} {artist_name}".strip(),
        }
    except Exception as e:
        print(f"[ytm_track_to_dict] error: {e}")
        return {}


# ─── Health ───────────────────────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "id-muzix-python"})


# ─── GET /trending ────────────────────────────────────────────────────────────
#
# ⚠️  KENAPA TIDAK PAKAI get_charts():
#   ytmusicapi v1.11.5+ mengubah get_charts() — sekarang return PLAYLIST OBJECTS
#   bukan individual tracks. Kalau tetap pakai, kita dapat data playlist bukan
#   lagu, dan thumbnail/videoId-nya kosong.
#
#   Fix proven dari project lain: gunakan search() multi-query sebagai proxy
#   trending. Hasilnya lebih konsisten dan ga kena breaking change.
#
@app.route("/trending")
def trending():
    cached = cache_get("trending_id")
    if cached:
        return jsonify(cached)

    TRENDING_QUERIES = [
        "trending musik indonesia 2026",
        "lagu viral indonesia 2026",
        "hits terbaru indonesia 2026",
    ]

    result   = []
    seen_ids = set()

    for query in TRENDING_QUERIES:
        if len(result) >= 20:
            break
        try:
            songs = ytmusic.search(query, filter="songs", limit=15)
            for song in songs:
                track = ytm_track_to_dict(song)
                vid   = track.get("videoId", "")
                if not vid or vid in seen_ids or not track.get("title"):
                    continue
                seen_ids.add(vid)
                track["rank"] = len(result) + 1
                result.append(track)
                if len(result) >= 20:
                    break
        except Exception as e:
            print(f"[Trending] query '{query}' gagal: {e}")
            continue

    if not result:
        return jsonify({"error": "Gagal memproses data trending"}), 500

    cache_set("trending_id", result, ttl=1800)
    return jsonify(result)


# ─── GET /related?q=title&artist=artist ───────────────────────────────────────
@app.route("/related")
def related():
    q      = request.args.get("q", "").strip()
    artist = request.args.get("artist", "").strip()
    if not q:
        return jsonify({"error": "q is required"}), 400

    cache_key = f"related_{q.lower()}_{artist.lower()}"
    cached    = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    try:
        search_query = f"{q} {artist}".strip()
        results      = ytmusic.search(search_query, filter="songs", limit=1)
        if not results:
            return jsonify([])

        video_id = results[0].get("videoId")
        if not video_id:
            return jsonify([])

        watch  = ytmusic.get_watch_playlist(videoId=video_id, limit=12)
        tracks = watch.get("tracks", [])[1:]

        related_songs = []
        for track in tracks:
            item = ytm_track_to_dict(track)
            if item.get("videoId") and item.get("title"):
                related_songs.append(item)

        cache_set(cache_key, related_songs, ttl=600)
        return jsonify(related_songs)

    except Exception as e:
        print(f"[Related] ERROR: {e}")
        return jsonify({"error": str(e)}), 500


# ─── GET /metadata?q=query ────────────────────────────────────────────────────
@app.route("/metadata")
def metadata():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "q is required"}), 400

    cache_key = f"meta_{q.lower()}"
    cached    = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    try:
        results = ytmusic.search(q, filter="songs", limit=1)
        if not results:
            return jsonify({"error": "not found"}), 404

        track = ytm_track_to_dict(results[0])
        if not track:
            return jsonify({"error": "Gagal parse track"}), 500

        cache_set(cache_key, track, ttl=3600)
        return jsonify(track)

    except Exception as e:
        print(f"[Metadata] ERROR: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
