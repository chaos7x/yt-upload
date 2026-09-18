# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install test/lint dependencies
pip install -r requirements-test.txt

# Run the full test suite
pytest -v

# Run a single test
pytest tests/test_youtube_api.py::TestGetAccessToken::test_success_returns_access_token_and_sends_refresh_token -v

# Lint (CI's actual scope — see "Lint scope" below)
ruff check src/yt_upload/ src/get_token/

# Bare-metal install (also installs the `yt-upload` and `get-token` console scripts)
pip install --break-system-packages --no-deps .

# Build a Docker image (name/version read from pyproject.toml; second arg picks the Dockerfile)
./build.sh                    # Standard Dockerfile
./build.sh 2.0.0 alpine       # Explicit version, Dockerfile.alpine
./build.sh 2.0.0 pyimg        # Dockerfile.pyimg

# Build standalone self-contained .pyz zipapps (one per [project.scripts] entry)
./build-pyz.sh                # produces yt-upload.pyz and get-token.pyz
```

CI (`.github/workflows/ci.yml`) runs `pytest -v` then `ruff check src/yt_upload/ src/get_token/` on every PR and push to `main`.

### Lint scope

`ruff check` covers `src/yt_upload/` and `src/get_token/` (matches CI). `tests/` is not part of that convention and may carry pre-existing, separately-tracked lint issues — don't be surprised if `ruff check .` (whole repo) turns up things outside the scope of whatever you're working on.

## Architecture

Two independently installable packages live under `src/`, both wired into `[project.scripts]` in `pyproject.toml`:

- **`yt_upload`** — the actual daemon/CLI, installed as the `yt-upload` command.
- **`get_token`** — a standalone, one-shot OAuth setup script, installed as `get-token`. It deliberately does **not** import from `yt_upload` (Docker copies `src/get_token/get_token.py` as a bare file straight to `/usr/local/bin/get_token`, independent of the pip-installed package), so anything it needs from the same logic (e.g. `client_secrets.json` parsing precedence in `load_client_secrets()`) is intentionally duplicated with a comment pointing at the `yt_upload` counterpart it must stay in sync with (`youtube_api.get_access_token()`).

### `yt_upload` module responsibilities

- `main.py` — argparse CLI entry point, three mutually exclusive modes: manual file(s) (`yt-upload video1.mp4 video2.mp4 ...`), auto-batch (`-a`, drains `IN_DIR` once), daemon (`-D`, persistent inotify watch). `_resolve_file_args()` builds a per-file `argparse.Namespace` copy so `--title-template` numbering doesn't mutate the shared `args` across a multi-file batch.
- `config.py` — all runtime settings live as module globals, loaded by `load_configuration()` with precedence: hardcoded defaults → `/etc/yt-upload/upload.conf` → `conf.d/*.conf` → environment variables (highest). Other modules always reference values as `config.NAME` (never `from yt_upload.config import NAME`) so a later `load_configuration()` hot-reload is actually seen; it's polled on a throttle from the daemon loop. Container vs. bare-metal defaults are chosen via `os.path.exists("/app/oauth")` / `os.path.exists("/videos")` checks (mirrored in `get_token.py`'s own container detection).
- `daemon.py` — inotify-based watch (`IN_CLOSE_WRITE` / `IN_MOVED_TO` only, to avoid a self-feeding event loop from the daemon's own directory scans) with a polling fallback when the `inotify` package isn't installed, plus a `flock`-based single-instance lock.
- `pipeline.py` — `process_single_file()` is the per-file state machine: validate → move to `WORK_DIR` → extract metadata/thumbnail → split if >10h → upload each segment → move to `DONE_DIR`/`CORRUPT_DIR`/`RETRY_DIR`. It **never raises** on an upload failure — it internally routes the file and returns — which is why both auto-batch and the multi-file CLI mode can loop over files with no try/except of their own.
- `media.py` — all `ffprobe`/`ffmpeg` calls: metadata/thumbnail extraction, readiness checks (file still being written), and lossless splitting via stream-copy segment muxing above `SEGMENT_TIME_SEC` (10h).
- `youtube_api.py` — hand-rolled resumable upload against the YouTube Data API v3 REST endpoints directly (no `google-api-python-client`). Every entry point (`get_access_token`, `upload_single_video`, `add_video_to_playlist`) takes an optional `cred_file`/`client_secrets_file` override, so per-call credential selection already works at this layer.
- `fileutils.py` — target-path resolution (`ALLOW_OVERWRITE` vs. `unique_path()`), and the segment-progress JSON sidecar (`.{filename}.progress.json`) that lets a failed multi-segment upload resume without re-uploading already-succeeded segments — this is also why `RETRY_DIR` and `CORRUPT_DIR` are separate (a file only goes to `CORRUPT_DIR` if *zero* segments succeeded).
- `text_utils.py` — sanitizing for the API, YouTube category name→ID mapping, and the description-blacklist censor.

### Config/CLI precedence pattern

Most per-upload settings (title, description, category, privacy, embeddable, …) follow the same override chain in `pipeline.py`: explicit CLI arg → container-extracted metadata (ffprobe tags) → `config.py` default. When adding a new overridable setting, follow this same three-way `args.x if ... else meta[...] or config.X` pattern rather than inventing a new one.

### Tests

`tests/conftest.py` exposes one session-scoped fixture per submodule (`config`, `text_utils`, `fileutils`, `media`, `youtube_api`, `pipeline`, `daemon`, `logging_setup`, `healthcheck`, `main_module`, `get_token_module`) instead of one fixture for the whole package — tests should import the specific submodule fixture that owns the function under test. A `reset_config_after_test` autouse fixture snapshots and restores `yt_upload.config`'s module globals after every test, since several tests patch config values directly.

## Backlog / Ideen

- **Multi-Channel-Support:** Mehrere YouTube-Kanäle gleichzeitig unterstützen; anhand von Video-Tags automatisch entscheiden, auf welchen Kanal hochgeladen wird. `youtube_api.upload_single_video()`/`get_access_token()` nehmen bereits pro Aufruf einen `cred_file`-Override entgegen — die API-seitige Grundlage existiert also schon. Was fehlt: mehrere benannte Credential-Dateien (get-token bräuchte einen wählbaren Output-Pfad statt der fixen `youtube-upload-credentials.json`), ein Konfigurationsschema für Tag→Kanal-Zuordnung (analog zur bestehenden `[blacklist]`-Sektion in `upload.conf`), und Routing-Logik in `pipeline.py`, die die Tags gegen diese Zuordnung matcht. Offene Designfragen: Priorität bei mehreren/keinen passenden Tags (Default-Kanal?), und wie sich das mit den bereits kanalgebundenen dynamischen Playlists verträgt.
