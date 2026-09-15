#!/usr/bin/env python3
import json
import subprocess

def get_video_metadata(file_path: str) -> dict:
    """Extrahiert alle Metadaten als sauberes Python-Dict."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        file_path
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    
    # Videostream herausfiltern
    v_stream = next((s for s in data.get("streams", []) if s["codec_type"] == "video"), {})
    
    return {
        "duration": float(data.get("format", {}).get("duration", 0)),
        "size_mb": int(data.get("format", {}).get("size", 0)) / (1024 * 1024),
        "width": v_stream.get("width"),
        "height": v_stream.get("height"),
        "codec": v_stream.get("codec_name")
    }

if __name__ == "__main__":
    # Beispiel für die Nutzung im Uploader:
    meta = get_video_metadata("/videos/test_part0.mkv")

    # Dynamischen Titel/Beschreibung für das YouTube-API-Snippet bauen
    youtube_snippet = {
        "snippet": {
            "title": f"Mein Video - Part 1 ({meta['height']}p)",
            "description": f"Automatisch hochgeladen.\nDauer: {meta['duration']:.1f}s | Codec: {meta['codec']}",
            "categoryId": "22"
        }
    }

    print(f"Bereit für Upload: {youtube_snippet['snippet']['title']} ({meta['size_mb']:.2f} MB)")
