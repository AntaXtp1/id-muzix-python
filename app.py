from flask import Flask, jsonify, request
from flask_cors import CORS
from ytmusicapi import YTMusic
import os
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

# ─── Helper: safe thumbnail extraction ───────────────────────────────────────
def get_thumbnail(song_obj, video_id=""):
    """
    Ambil thumbnail dari object lagu ytmusicapi.
    Coba 'thumbnails' (search/playlist) dan 'thumbnail' (watch playlist).
    Fallback ke YouTube direct URL jika ada videoId.
    """
    # ytmusicapi search results pake 'thumbnails' (list of dict)
    thumbs = song_obj.get("thumbnails") or song_obj.get("thumbnail") or []

    if isinstance(thumbs, list) and thumbs:
        last = thumbs[-1]
        if isinstance(last, dict) and last.get("url"):
            url = last["url"]
            # Clean up size param dari Google CDN (opsional, untuk resolusi lebih tinggi)
            # Format: https://lh3.googleusercontent.com/...=w226-h226-l90-rj
            # Ganti ukuran ke yang lebih gede kalau ada
            if "=w" in url and "-h" in url:
                base = url.split("=w")[0]
                return base + "=w500-h500-l90-rj"
            return url

    # Fallback: pakai YouTube thumbnail langsung via videoId
    vid = video_id or song_obj.get("videoId", "")
    if vid:
        return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    return ""

# ─── Health ───────────────────────────────────────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "service": "id-muzix-python"})

# ─── GET /trending ────────────────────────────────────────────────────────────
@app.route("/trending")
def trending():
    cached = cache_get("trending_id")
    if cached:
        return jsonify(cached)

    result = []

    # ── Strategy 1: get_charts('ID') → ambil playlist → get tracks ───────────
    try:
        charts = ytmusic.get_charts(country="ID")
        # 'videos' berisi list playlist chart (bukan individual songs)
        chart_videos = charts.get("videos", [])

        if isinstance(chart_videos, list) and chart_videos:
            playlist_id = chart_videos[0].get("playlistId", "")
            if playlist_id:
                playlist = ytmusic.get_playlist(playlist_id, limit=20)
                tracks = playlist.get("tracks", [])

                for i, track in enumerate(tracks):
                    try:
                        title    = track.get("title", "")
                        artists  = track.get("artists", []) or []
                        artist   = ""
                        if isinstance(artists, list) and artists:
                            first  = artists[0]
                            artist = first.get("name", "") if isinstance(first, dict) else str(first)

                        video_id  = track.get("videoId", "")
                        thumbnail = get_thumbnail(track, video_id)

                        if not title:
                            continue

                        result.append({
                            "rank":      i + 1,
                            "title":     title,
                            "artist":    artist,
                            "thumbnail": thumbnail,
                            "videoId":   video_id,
                            "query":     f"{title} {artist}".strip(),
                        })
                    except Exception as e:
                        print(f"[Charts/Playlist] skip track {i}: {e}")
                        continue

        print(f"[Charts] berhasil: {len(result)} lagu dari chart ID")

    except Exception as e:
        print(f"[Charts] get_charts gagal: {e}, lanjut ke fallback search")

    # ── Strategy 2: fallback ke multiple search queries ───────────────────────
    if len(result) < 10:
        search_queries = [
            "lagu viral tiktok indonesia terbaru",
            "top hits indonesia 2025",
            "lagu pop indonesia populer 2025",
        ]
        seen_titles = {r["title"].lower() for r in result}

        for sq in search_queries:
            if len(result) >= 20:
                break
            try:
                songs = ytmusic.search(sq, filter="songs", limit=20)
                for song in songs:
                    try:
                        title = song.get("title", "")
                        if not title or title.lower() in seen_titles:
                            continue

                        artists = song.get("artists", []) or []
                        artist  = ""
                        if isinstance(artists, list) and artists:
                            first  = artists[0]
                            artist = first.get("name", "") if isinstance(first, dict) else str(first)

                        video_id  = song.get("videoId", "")
                        thumbnail = get_thumbnail(song, video_id)

                        result.append({
                            "rank":      len(result) + 1,
                            "title":     title,
                            "artist":    artist,
                            "thumbnail": thumbnail,
                            "videoId":   video_id,
                            "query":     f"{title} {artist}".strip(),
                        })
                        seen_titles.add(title.lower())

                        if len(result) >= 20:
                            break
                    except Exception as e:
                        print(f"[Trending Fallback] skip item: {e}")
                        continue
            except Exception as e:
                print(f"[Trending Fallback] query '{sq}' gagal: {e}")
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
        tracks = watch.get("tracks", [])[1:]  # skip lagu pertama (lagu itu sendiri)

        related_songs = []
        for track in tracks:
            try:
                title     = track.get("title", "")
                artists   = track.get("artists", []) or []
                t_artist  = artists[0]["name"] if artists and isinstance(artists[0], dict) else ""
                vid       = track.get("videoId", "")
                thumbnail = get_thumbnail(track, vid)

                if not title:
                    continue

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

        song      = results[0]
        title     = song.get("title", q)
        artists   = song.get("artists", []) or []
        artist    = artists[0]["name"] if artists and isinstance(artists[0], dict) else ""
        album_obj = song.get("album") or {}
        album     = album_obj.get("name", "") if isinstance(album_obj, dict) else ""
        video_id  = song.get("videoId", "")
        thumbnail = get_thumbnail(song, video_id)

        result = {
            "title":     title,
            "artist":    artist,
            "album":     album,
            "thumbnail": thumbnail,
            "videoId":   video_id,
        }

        cache_set(cache_key, result, ttl=3600)
        return jsonify(result)

    except Exception as e:
        print(f"[Metadata] ERROR: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
