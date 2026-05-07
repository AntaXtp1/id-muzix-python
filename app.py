from flask import Flask, jsonify, request
from flask_cors import CORS
from ytmusicapi import YTMusic
import os
import time
import threading

app = Flask(__name__)
CORS(app)

# ─── Init YTMusic (tanpa auth = public data aja, cukup buat trending & search) ─
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

# ─── Health ───────────────────────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "id-muzix-python"})

# ─── GET /trending — chart Indonesia (Workaround pakai Search) ───────────────
@app.route("/trending")
def trending():
    cached = cache_get("trending_id")
    if cached:
        return jsonify(cached)

    try:
        # WORKAROUND: Kita akali dengan mencari playlist/lagu viral
        # karena get_charts() sering kosong/diblokir di server
        results = ytmusic.search("lagu viral tiktok indonesia terbaru", filter="songs", limit=20)

        if not results:
            return jsonify({"error": "Trending kosong, pencarian tidak menemukan hasil"}), 500

        result = []
        for i, song in enumerate(results):
            try:
                title = song.get("title", "")
                
                # Parsing artist dengan aman
                artists = song.get("artists", [])
                if isinstance(artists, list) and artists:
                    first = artists[0]
                    artist = first.get("name", "") if isinstance(first, dict) else str(first)
                else:
                    artist = ""

                videoId = song.get("videoId", "")

                # Parsing thumbnail
                thumbnails = song.get("thumbnails", [])
                thumbnail = thumbnails[-1]["url"] if thumbnails and isinstance(thumbnails[-1], dict) else ""

                # Parsing album
                album_obj = song.get("album", {})
                album = album_obj.get("name", "") if isinstance(album_obj, dict) else ""

                if not title:
                    continue

                result.append({
                    "rank":      i + 1,
                    "title":     title,
                    "artist":    artist,
                    "album":     album,
                    "thumbnail": thumbnail,
                    "videoId":   videoId,
                    "query":     f"{title} {artist}".strip(),
                })
            except Exception as e:
                print(f"[Trending] skip item {i}: {e}")
                continue

        if not result:
            return jsonify({"error": "Gagal memproses data trending"}), 500

        cache_set("trending_id", result, ttl=1800) # Cache 30 menit
        return jsonify(result)

    except Exception as e:
        print(f"[Trending] ERROR: {e}")
        return jsonify({"error": str(e)}), 500

# ─── GET /related?q=title&artist=artist — related songs ───────────────────────
@app.route("/related")
def related():
    q      = request.args.get("q", "").strip()
    artist = request.args.get("artist", "").strip()
    if not q:
        return jsonify({"error": "q is required"}), 400

    cache_key = f"related_{q.lower()}_{artist.lower()}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    try:
        # Search lagu, ambil yang paling relevan
        search_query = f"{q} {artist}".strip()
        results = ytmusic.search(search_query, filter="songs", limit=1)

        if not results:
            return jsonify([])

        video_id = results[0].get("videoId")
        if not video_id:
            return jsonify([])

        # Ambil watch playlist (related songs dari YT Music radio)
        watch = ytmusic.get_watch_playlist(videoId=video_id, limit=10)
        tracks = watch.get("tracks", [])[1:]  # skip lagu pertama (lagu itu sendiri)

        related_songs = []
        for track in tracks:
            try:
                title      = track.get("title", "")
                artists    = track.get("artists", [])
                t_artist   = artists[0]["name"] if artists else ""
                vid        = track.get("videoId", "")
                thumbnails = track.get("thumbnail", [])
                thumbnail  = thumbnails[-1]["url"] if thumbnails else ""

                related_songs.append({
                    "title":     title,
                    "artist":    t_artist,
                    "thumbnail": thumbnail,
                    "videoId":   vid,
                    "query":     f"{title} {t_artist}".strip(),
                })
            except Exception as e:
                print(f"[Related] skip track: {e}")
                continue

        cache_set(cache_key, related_songs, ttl=600)  # cache 10 menit
        return jsonify(related_songs)

    except Exception as e:
        print(f"[Related] ERROR: {e}")
        return jsonify({"error": str(e)}), 500

# ─── GET /metadata?q=query — thumbnail + artist + album dari YT Music ─────────
@app.route("/metadata")
def metadata():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "q is required"}), 400

    cache_key = f"meta_{q.lower()}"
    cached = cache_get(cache_key)
    if cached:
        return jsonify(cached)

    try:
        results = ytmusic.search(q, filter="songs", limit=1)
        if not results:
            return jsonify({"error": "not found"}), 404

        song       = results[0]
        title      = song.get("title", q)
        artists    = song.get("artists", [])
        artist     = artists[0]["name"] if artists else ""
        album_obj  = song.get("album") or {}
        album      = album_obj.get("name", "") if isinstance(album_obj, dict) else ""
        videoId    = song.get("videoId", "")
        thumbnails = song.get("thumbnails", [])
        # Ambil thumbnail resolusi tertinggi
        thumbnail  = thumbnails[-1]["url"] if thumbnails else ""

        result = {
            "title":     title,
            "artist":    artist,
            "album":     album,
            "thumbnail": thumbnail,
            "videoId":   videoId,
        }

        cache_set(cache_key, result, ttl=3600)  # cache 1 jam
        return jsonify(result)

    except Exception as e:
        print(f"[Metadata] ERROR: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
