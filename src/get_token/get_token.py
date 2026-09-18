#!/usr/bin/env python3
# ruff: noqa: EXE001 - Shebang ist reine Bequemlichkeit für optionale direkte
# Ausführung (./get_token.py); das eigentliche Ausführbar-Bit übersteht
# Git-Checkouts/Web-Uploads nicht zuverlässig, das Skript läuft ganz normal
# auch via `python3 get_token.py` ohne +x.

import json
import os
import secrets
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import requests

# Standardmäßig AUS: der klassische Copy-Paste-Flow (URL manuell aus dem
# Browser einfügen) funktioniert unabhängig davon, ob get-token und der
# Browser auf demselben Rechner laufen - das ist der übliche Fall, wenn
# get-token per `docker run -it ... get_token` auf einem entfernten
# Server/NAS ausgeführt wird, während der Browser auf einem anderen Gerät
# läuft. Der lokale Callback-Server (OAUTH_LOCAL_SERVER=true) fängt den
# Redirect zwar automatisch ab und erspart das Copy-Paste, funktioniert aber
# NUR, wenn der Browser tatsächlich http://localhost:8080/ auf demselben
# Rechner erreichen kann (z.B. Bare-Metal-Desktop-Nutzung, oder Docker mit
# explizit publiziertem Port 8080) - daher bewusst Opt-in per Env-Var statt
# neuer Standard.
USE_LOCAL_SERVER = os.environ.get("OAUTH_LOCAL_SERVER", "").strip().lower() in ("true", "yes", "1", "on")

# Gleiche Container-Erkennung wie yt_upload.config (os.path.exists("/app/oauth")):
# Docker mountet die OAuth-Secrets/Credentials dorthin. Bare-Metal hat keinen
# solchen fixen Pfad - dort ist der einzige sinnvolle Default das aktuelle
# Arbeitsverzeichnis (relativer Pfad), da get-token im Gegensatz zum
# dauerhaft laufenden yt-upload-Dienst ein einmaliges, interaktiv vom Nutzer
# aus einem Terminal aufgerufenes Setup-Tool ist - ein fixer, am
# Installationsort (z.B. site-packages) orientierter Pfad wäre dort weder
# auffindbar noch beschreibbar. Per Umgebungsvariable überschreibbar, damit
# beide Skripte bei Bedarf explizit auf dieselbe Datei zeigen können.
_IN_CONTAINER = os.path.exists("/app/oauth")
CLIENT_SECRETS_FILE = os.environ.get(
    "CLIENT_SECRETS_FILE",
    "/app/oauth/client_secrets.json" if _IN_CONTAINER else "client_secrets.json",
)
OUTPUT_CREDENTIALS_FILE = os.environ.get(
    "CREDENTIALS_FILE",
    "/app/oauth/youtube-upload-credentials.json" if _IN_CONTAINER else "youtube-upload-credentials.json",
)
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube"
]


def load_client_secrets():
    with open(CLIENT_SECRETS_FILE, "r", encoding="utf-8") as secrets_file:
        data = json.load(secrets_file)

    # Gleiche Präzedenz wie yt_upload.youtube_api.get_access_token() beim Lesen
    # von client_secrets.json (flache Top-Level-Felder vor installed/web-Wrapper):
    # get_token.py wird im Dockerfile als eigenständige Datei nach
    # /usr/local/bin/get_token kopiert und kann daher nicht vom yt_upload-Package
    # importieren, soll dieselbe Datei aber identisch interpretieren wie dieses.
    # `or` statt `dict.get(key, default)` sorgt zusätzlich dafür, dass ein leerer
    # String (z.B. "auth_uri": "") ebenfalls auf den Google-Default zurückfällt.
    nested = data.get("installed") or data.get("web") or {}
    client_id = data.get("client_id") or nested.get("client_id")
    client_secret = data.get("client_secret") or nested.get("client_secret")
    auth_uri = data.get("auth_uri") or nested.get("auth_uri") or "https://accounts.google.com/o/oauth2/v2/auth"
    token_uri = data.get("token_uri") or nested.get("token_uri") or "https://oauth2.googleapis.com/token"

    if not client_id or not client_secret:
        raise ValueError("client_id oder client_secret fehlt in der Client-Secrets-Datei.")

    return client_id, client_secret, auth_uri, token_uri


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """
    Fängt genau einen OAuth-Redirect lokal ab (nur bei OAUTH_LOCAL_SERVER=true).
    BaseHTTPRequestHandler wird von HTTPServer pro Request neu instanziiert und
    kann daher keinen eigenen Zustand über den Request hinaus halten - der
    Query-String wird deshalb auf dem (langlebigen) server-Objekt abgelegt.
    """

    def do_GET(self):
        self.server.callback_query = urlparse(self.path).query
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<html><body><h1>Authentifizierung abgeschlossen.</h1>"
            "<p>Du kannst dieses Fenster jetzt schließen und zum Terminal zurückkehren.</p>"
            "</body></html>".encode("utf-8")
        )

    def log_message(self, fmt, *args):
        pass  # Unterdrückt BaseHTTPRequestHandlers Standard-Zugriffslog auf stderr


def _await_redirect_via_local_server(redirect_uri):
    """
    Startet einen einmaligen lokalen HTTP-Server auf redirect_uri und blockiert,
    bis genau ein Request eintrifft (der OAuth-Redirect von Google). Gibt dessen
    Query-String zurück. Siehe OAUTH_LOCAL_SERVER-Kommentar weiter oben zu den
    Voraussetzungen (Browser muss redirect_uri auf demselben Rechner erreichen).
    """
    parsed = urlparse(redirect_uri)
    try:
        server = HTTPServer((parsed.hostname, parsed.port), _OAuthCallbackHandler)
    except OSError as e:
        raise RuntimeError(
            f"Konnte lokalen OAuth-Callback-Server nicht auf {redirect_uri} starten ({e}). "
            "Läuft bereits ein anderer Prozess auf diesem Port? Falls get-token auf einem "
            "anderen Rechner als der Browser läuft (z.B. Docker/SSH auf einem Server), "
            "funktioniert OAUTH_LOCAL_SERVER ohnehin nicht - Variable nicht setzen."
        ) from e

    server.callback_query = None
    try:
        server.handle_request()
    finally:
        server.server_close()
    return server.callback_query or ""


def get_authorization_code(auth_uri, token_uri, client_id, redirect_uri, client_secret):
    state = secrets.token_urlsafe(32)
    auth_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = f"{auth_uri}?{urlencode(auth_params)}"

    if USE_LOCAL_SERVER:
        print("\n1. Öffne diesen Link im Browser (öffnet sich ggf. automatisch):\n")
        print(auth_url)
        print("\n----------------------------------------------------------------")
        print("2. Logge dich ein und erlaube den Zugriff.")
        print(f"3. OAUTH_LOCAL_SERVER ist aktiv: {redirect_uri} wird automatisch abgefangen,")
        print("   kein Copy-Paste der URL nötig. Warte auf die Weiterleitung...")
        print("----------------------------------------------------------------\n")
        webbrowser.open(auth_url)
        query = parse_qs(_await_redirect_via_local_server(redirect_uri))
    else:
        print("\n1. Öffne diesen Link im Browser:\n")
        print(auth_url)
        print("\n----------------------------------------------------------------")
        print("2. Logge dich ein und erlaube den Zugriff.")
        print("3. Nach dem Klick auf 'Zulassen' bricht der Browser ab (Seite nicht gefunden).")
        print("4. Kopiere die KOMPLETTE URL aus der Adresszeile.")
        print("----------------------------------------------------------------\n")

        redirect_response = input("Füge die kopierte URL hier ein: ").strip()
        query = parse_qs(urlparse(redirect_response).query)

    if query.get("error"):
        raise RuntimeError(f"Google OAuth wurde abgebrochen: {query['error'][0]}")
    if query.get("state", [None])[0] != state:
        raise RuntimeError("Ungültiger OAuth-State. Bitte den Vorgang erneut starten.")
    if not query.get("code", [None])[0]:
        raise RuntimeError("Keine OAuth-Autorisierung in der kopierten URL gefunden.")

    token_response = requests.post(
        token_uri,
        data={
            "code": query["code"][0],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if token_response.status_code != 200:
        raise RuntimeError(f"Token-Austausch fehlgeschlagen: {token_response.text}")

    return token_response.json()


def main():
    try:
        # Zielverzeichnis automatisch anlegen, falls es noch nicht existiert.
        # os.path.dirname() liefert bei einem reinen Dateinamen ohne
        # Verzeichnisanteil (Bare-Metal-Default, relativ zum aktuellen
        # Arbeitsverzeichnis) einen leeren String zurück - os.makedirs("")
        # würde damit crashen, ist hier aber ohnehin unnötig (das aktuelle
        # Arbeitsverzeichnis existiert bereits).
        target_dir = os.path.dirname(OUTPUT_CREDENTIALS_FILE)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)

        client_id, client_secret, auth_uri, token_uri = load_client_secrets()
        redirect_uri = "http://localhost:8080/"
        token_response = get_authorization_code(auth_uri, token_uri, client_id, redirect_uri, client_secret)
        refresh_token = token_response.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("Google hat keinen Refresh-Token geliefert. Bitte den OAuth-Zugriff erneut erlauben.")
        
        # Datumsformatierung mit Fallback, falls credentials.expiry None ist
        expires_in = int(token_response.get("expires_in", 3600))
        expiry_str = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Tatsächlich von Google gewährten Scope verwenden statt blind der
        # angeforderten SCOPES, falls Google (z.B. bei partieller Zustimmung)
        # einen engeren Scope zurückliefert.
        granted_scope = token_response.get("scope") or " ".join(SCOPES)
        granted_scopes = granted_scope.split()

        legacy_credentials = {
            "access_token": token_response.get("access_token"),
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "token_expiry": expiry_str,
            "token_uri": token_uri,
            "user_agent": None,
            "revoke_uri": "https://oauth2.googleapis.com/revoke",
            "id_token": None,
            "id_token_jwt": None,
            "token_response": {
                "access_token": token_response.get("access_token"),
                "expires_in": expires_in,
                "refresh_token": refresh_token,
                "scope": granted_scope,
                "token_type": token_response.get("token_type", "Bearer")
            },
            "scopes": granted_scopes,
            "token_info_uri": "https://oauth2.googleapis.com/tokeninfo",
            "invalid": False,
            "_class": "OAuth2Credentials",
            "_module": "oauth2client.client"
        }
        
        # Datei direkt mit Modus 600 anlegen (statt nachträglichem chmod), damit sie
        # zu keinem Zeitpunkt mit dem Standard-umask (z.B. 644) für Gruppe/Andere
        # lesbar auf der Platte liegt - die Datei enthält Client-Secret & Refresh-Token
        # im Klartext.
        fd = os.open(OUTPUT_CREDENTIALS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(legacy_credentials, f, indent=2)

        print(f"\n[ERFOLG] {OUTPUT_CREDENTIALS_FILE} wurde erfolgreich erstellt!")

    except KeyboardInterrupt:
        print("\n\n[ABBRUCH] Vorgang durch Benutzer abgebrochen (Strg + C). Es wurde nichts gespeichert.")
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as e:
        print(f"\n[FEHLER] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
