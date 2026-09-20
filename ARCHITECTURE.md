# 🏗️ Architektur

Dieses Dokument beschreibt den Aufbau von `yt-upload` auf Modulebene: welche Komponente was tut und wie Dateien, Konfiguration und externe Dienste (FFmpeg, YouTube/Google) zusammenspielen. Für Betriebsanleitungen (Docker, CLI-Flags, `upload.conf`) siehe [README.md](README.md); für Entwicklungs-Konventionen (Test-Fixtures, Config-Precedence-Pattern) siehe [CLAUDE.md](CLAUDE.md).

## Überblick

```mermaid
flowchart TD

subgraph group_entry["Entry And Operations"]
  node_cli_modes["CLI Modes<br/>[main.py]"]
  node_folder_watcher["Folder Watcher<br/>[daemon.py]"]
  node_healthcheck["Healthcheck<br/>[healthcheck.py]"]
end

subgraph group_media["Media Processing"]
  node_video_pipeline["Video Pipeline<br/>[pipeline.py]"]
  node_validator["Media Validator<br/>[media.py]"]
  node_metadata_thumbs["Metadata And Thumbs<br/>[media.py]"]
  node_segmenter["Video Segmenter<br/>[media.py]"]
  node_text_policy["Text Policy<br/>[text_utils.py]"]
end

subgraph group_delivery["YouTube Delivery"]
  node_youtube_uploader["YouTube Uploader<br/>[youtube_api.py]"]
  node_oauth_tool["OAuth Token Tool<br/>[get_token.py]"]
end

subgraph group_state["Configuration And State"]
  node_config["Runtime Configuration<br/>[config.py]"]
  node_media_folders["Media Folders<br/>[config.py]"]
  node_progress_state["Segment Progress<br/>[fileutils.py]"]
  node_heartbeat["Heartbeat File<br/>[healthcheck.py]"]
  node_oauth_credentials["OAuth Credentials<br/>[get_token.py]"]
end

node_operator(("Operator"))
node_video_producer(("Video Producer"))
node_health_monitor(("Health Monitor"))
node_google_oauth["Google OAuth"]
node_youtube_service["YouTube API"]
node_ffmpeg["FFmpeg And FFprobe"]

node_operator -->|"invokes"| node_cli_modes
node_video_producer -->|"writes videos"| node_media_folders
node_cli_modes -->|"loads config"| node_config
node_cli_modes -->|"starts daemon"| node_folder_watcher
node_cli_modes -->|"dispatches jobs"| node_video_pipeline
node_health_monitor -->|"runs check"| node_healthcheck
node_folder_watcher -->|"watches input"| node_media_folders
node_folder_watcher -->|"writes heartbeat"| node_heartbeat
node_video_pipeline -->|"reads settings"| node_config
node_video_pipeline -->|"validates media"| node_validator
node_video_pipeline -->|"extracts metadata"| node_metadata_thumbs
node_video_pipeline -->|"splits videos"| node_segmenter
node_video_pipeline -->|"sanitizes text"| node_text_policy
node_video_pipeline -->|"moves files"| node_media_folders
node_video_pipeline -->|"reads writes"| node_progress_state
node_video_pipeline -->|"uploads segments"| node_youtube_uploader
node_validator -->|"probes media"| node_ffmpeg
node_metadata_thumbs -->|"extracts frames"| node_ffmpeg
node_segmenter -->|"splits losslessly"| node_ffmpeg
node_youtube_uploader -->|"reads credentials"| node_oauth_credentials
node_youtube_uploader -->|"uploads videos"| node_youtube_service
node_oauth_tool -->|"requests consent"| node_google_oauth
node_oauth_tool -->|"writes tokens"| node_oauth_credentials
node_healthcheck -->|"reads freshness"| node_heartbeat

click node_cli_modes "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/main.py"
click node_folder_watcher "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/daemon.py"
click node_healthcheck "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/healthcheck.py"
click node_video_pipeline "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/pipeline.py"
click node_validator "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/media.py"
click node_metadata_thumbs "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/media.py"
click node_segmenter "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/media.py"
click node_text_policy "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/text_utils.py"
click node_youtube_uploader "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/youtube_api.py"
click node_oauth_tool "https://github.com/chaos7x/yt-upload/blob/main/src/get_token/get_token.py"
click node_config "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/config.py"
click node_media_folders "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/config.py"
click node_progress_state "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/fileutils.py"
click node_heartbeat "https://github.com/chaos7x/yt-upload/blob/main/src/yt_upload/healthcheck.py"
click node_oauth_credentials "https://github.com/chaos7x/yt-upload/blob/main/src/get_token/get_token.py"

classDef toneNeutral fill:#f8fafc,stroke:#334155,stroke-width:1.5px,color:#0f172a
classDef toneBlue fill:#dbeafe,stroke:#2563eb,stroke-width:1.5px,color:#172554
classDef toneAmber fill:#fef3c7,stroke:#d97706,stroke-width:1.5px,color:#78350f
classDef toneMint fill:#dcfce7,stroke:#16a34a,stroke-width:1.5px,color:#14532d
classDef toneRose fill:#ffe4e6,stroke:#e11d48,stroke-width:1.5px,color:#881337
classDef toneIndigo fill:#e0e7ff,stroke:#4f46e5,stroke-width:1.5px,color:#312e81
classDef toneTeal fill:#ccfbf1,stroke:#0f766e,stroke-width:1.5px,color:#134e4a
class node_cli_modes,node_folder_watcher,node_healthcheck toneBlue
class node_video_pipeline,node_validator,node_metadata_thumbs,node_segmenter,node_text_policy toneAmber
class node_youtube_uploader,node_oauth_tool,node_youtube_service toneMint
class node_config,node_media_folders,node_progress_state,node_heartbeat,node_oauth_credentials toneRose
class node_operator,node_video_producer,node_health_monitor,node_google_oauth,node_ffmpeg toneIndigo
```

## Komponenten

### Einstieg & Betrieb

- **`main.py`** — argparse-CLI, drei sich gegenseitig ausschließende Modi: manuelle Datei(en) (`yt-upload video1.mp4 ...`), Auto-Batch (`-a`, verarbeitet `IN_DIR` einmalig), Dämon (`-D`, dauerhafter inotify-Watch). `_resolve_file_args()` erzeugt pro Datei eine eigene `argparse.Namespace`-Kopie, damit die `--title-template`-Nummerierung bei mehreren Dateien nicht das gemeinsame `args`-Objekt mutiert. `ensure_directories()` läuft nur für `-a`/`-D`: der manuelle Datei-Modus verarbeitet die übergebene(n) Datei(en) an Ort und Stelle (siehe `pipeline.py` unten) und braucht `IN_DIR`/`WORK_DIR`/`DONE_DIR`/`CORRUPT_DIR`/`RETRY_DIR` daher gar nicht erst.
- **`config.py`** — alle Laufzeit-Einstellungen liegen als Modul-globale Variablen, geladen von `load_configuration()` mit der Prioritätskette Hardcoded-Defaults → `/etc/yt-upload/upload.conf` → `conf.d/*.conf` → Umgebungsvariablen (höchste Priorität). Andere Module referenzieren Werte immer als `config.NAME` (nie `from yt_upload.config import NAME`), damit ein späteres Hot-Reload durch `load_configuration()` auch tatsächlich ankommt. `CREDENTIALS_FILE`s Container- vs. Bare-Metal-Default wird über `os.path.exists("/app/oauth")` unterschieden; `IN_DIR`/`WORK_DIR`/`DONE_DIR`/`CORRUPT_DIR`/`RETRY_DIR` zeigen einheitlich (kein Unterschied mehr zwischen Docker und Bare-Metal) auf `/srv/media-pipeline/*`.
- **`daemon.py`** — inotify-basierter Watch (ausschließlich `IN_CLOSE_WRITE`/`IN_MOVED_TO`, um eine sich selbst befeuernde Event-Schleife durch die eigenen Verzeichnis-Scans zu vermeiden) mit Polling-Fallback, falls das `inotify`-Paket fehlt, plus ein `flock`-basierter Single-Instance-Lock (`acquire_instance_lock()`).
- **`healthcheck.py`** — `write_heartbeat()` aktualisiert regelmäßig (auch innerhalb langer Einzeloperationen wie einem mehrstündigen Resumable-Upload) eine Heartbeat-Datei; `run_healthcheck()` prüft nur deren Existenz/Alter, lädt bewusst keine Config und ist damit für häufige externe Aufrufe (Docker `HEALTHCHECK`) geeignet.

### Medienverarbeitung

- **`pipeline.py`** — `process_single_file()` ist die Zustandsmaschine pro Datei, gesteuert über den Parameter `manage_files` (Standard `True`, von Auto-Batch/Dämon verwendet): validieren → nach `WORK_DIR` verschieben → Metadaten/Thumbnail extrahieren → bei >10h splitten → jedes Segment hochladen → nach `DONE_DIR`/`CORRUPT_DIR`/`RETRY_DIR` verschieben. Der manuelle CLI-Modus ruft mit `manage_files=False` auf: die Quelldatei bleibt exakt liegen (kein Move nach WORK/DONE/CORRUPT/RETRY), ein Splitting bei Überlänge schreibt seine Segmente stattdessen in ein per `tempfile.mkdtemp()` erzeugtes Temp-Verzeichnis. Die Funktion wirft bei einem Upload-Fehler **nie** eine Exception — sie routet/loggt das Ergebnis intern selbst und kehrt zurück —, weshalb sowohl Auto-Batch als auch der Multi-Datei-CLI-Modus ohne eigenes try/except über Dateien iterieren können.
- **`media.py`** — sämtliche `ffprobe`/`ffmpeg`-Aufrufe: Metadaten-/Thumbnail-Extraktion, Bereitschaftsprüfung (Datei wird noch geschrieben?) und verlustfreies Splitting per Stream-Copy-Segmentierung oberhalb von `SEGMENT_TIME_SEC` (10h).
- **`text_utils.py`** — Sanitizing für die API, Mapping YouTube-Kategoriename → ID, sowie der Beschreibungs-Blacklist-Zensor.

### YouTube-Integration

- **`youtube_api.py`** — handgeschriebener Resumable-Upload direkt gegen die YouTube-Data-API-v3-REST-Endpunkte (ohne `google-api-python-client`). Jeder Einstiegspunkt (`get_access_token`, `upload_single_video`, `add_video_to_playlist`) nimmt optional einen `cred_file`/`client_secrets_file`-Override entgegen — die API-seitige Grundlage für spätere Mehr-Kanal-Unterstützung existiert also bereits (siehe Backlog in [CLAUDE.md](CLAUDE.md)).
- **`get_token.py`** (Package `get_token`, eigener Einstiegspunkt `get-token`) — eigenständiges, einmaliges OAuth-Setup-Skript. Es importiert bewusst **nicht** aus `yt_upload` (Docker kopiert die Datei als eigenständiges Skript nach `/usr/local/bin/get_token`, unabhängig vom pip-Package), weshalb gemeinsame Logik wie die `client_secrets.json`-Parsing-Präzedenz absichtlich dupliziert ist — mit Kommentar-Verweis auf das `yt_upload`-Gegenstück (`youtube_api.get_access_token()`), mit dem sie synchron gehalten werden muss.

### Dateien & Zustand

- **`fileutils.py`** — Ziel-Pfad-Auflösung (`ALLOW_OVERWRITE` vs. `unique_path()`) sowie das Segment-Fortschritts-JSON-Sidecar (`.{filename}.progress.json`), das einem fehlgeschlagenen Multi-Segment-Upload erlaubt, ohne erneuten Upload bereits erfolgreicher Segmente fortzusetzen. Aus demselben Grund sind `RETRY_DIR` und `CORRUPT_DIR` getrennt: eine Datei landet nur dann in `CORRUPT_DIR`, wenn *kein einziges* Segment erfolgreich war.
- **Video-Verzeichnisse** (`IN_DIR`/`WORK_DIR`/`DONE_DIR`/`CORRUPT_DIR`/`RETRY_DIR`) — der Dateisystem-Zustand, entlang dessen `pipeline.py` jede Datei bewegt; Pfade kommen aus `config.py` (siehe [README.md](README.md#-verzeichnisstruktur-im-container)).
- **OAuth-Credentials** — von `get_token.py` per interaktivem Flow erzeugt, von `youtube_api.py` gelesen und (Token-Refresh) aktualisiert.
- **Heartbeat-Datei** — von `healthcheck.py` geschrieben/gelesen, Grundlage für `--healthcheck` und Docker/Kubernetes-Liveness-Probes.

## Config/CLI-Präzedenz-Pattern

Die meisten Pro-Upload-Einstellungen (Titel, Beschreibung, Kategorie, Privacy, Embeddable, …) folgen in `pipeline.py` derselben Override-Kette: expliziter CLI-Parameter → aus dem Container extrahierte Metadaten (ffprobe-Tags) → `config.py`-Default (`args.x if ... else meta[...] or config.X`). Neue überschreibbare Einstellungen sollten diesem Muster folgen statt ein neues zu erfinden.
