#!/usr/bin/env python3
# Copyright (C) 2026 Chaos7x
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.

import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import requests

CLIENT_SECRETS_FILE = "/app/oauth/client_secrets.json"
OUTPUT_CREDENTIALS_FILE = "/app/oauth/youtube-upload-credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube"
]


def load_client_secrets():
    with open(CLIENT_SECRETS_FILE, "r", encoding="utf-8") as secrets_file:
        data = json.load(secrets_file)

    client_data = data.get("installed") or data.get("web") or data
    client_id = client_data.get("client_id")
    client_secret = client_data.get("client_secret")
    auth_uri = client_data.get("auth_uri", "https://accounts.google.com/o/oauth2/v2/auth")
    token_uri = client_data.get("token_uri", "https://oauth2.googleapis.com/token")

    if not client_id or not client_secret:
        raise ValueError("client_id oder client_secret fehlt in der Client-Secrets-Datei.")

    return client_id, client_secret, auth_uri, token_uri


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
        # Zielverzeichnis automatisch anlegen, falls es noch nicht existiert
        os.makedirs(os.path.dirname(OUTPUT_CREDENTIALS_FILE), exist_ok=True)

        client_id, client_secret, auth_uri, token_uri = load_client_secrets()
        redirect_uri = "http://localhost:8080/"
        token_response = get_authorization_code(auth_uri, token_uri, client_id, redirect_uri, client_secret)
        refresh_token = token_response.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("Google hat keinen Refresh-Token geliefert. Bitte den OAuth-Zugriff erneut erlauben.")
        
        # Datumsformatierung mit Fallback, falls credentials.expiry None ist
        expires_in = int(token_response.get("expires_in", 3600))
        expiry_str = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")

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
                "scope": " ".join(SCOPES),
                "token_type": token_response.get("token_type", "Bearer")
            },
            "scopes": SCOPES,
            "token_info_uri": "https://oauth2.googleapis.com/tokeninfo",
            "invalid": False,
            "_class": "OAuth2Credentials",
            "_module": "oauth2client.client"
        }
        
        with open(OUTPUT_CREDENTIALS_FILE, "w") as f:
            json.dump(legacy_credentials, f, indent=2)
        os.chmod(OUTPUT_CREDENTIALS_FILE, 0o600)
            
        print(f"\n[ERFOLG] {OUTPUT_CREDENTIALS_FILE} wurde erfolgreich erstellt!")

    except KeyboardInterrupt:
        print("\n\n[ABBRUCH] Vorgang durch Benutzer abgebrochen (Strg + C). Es wurde nichts gespeichert.")
        sys.exit(130)

if __name__ == "__main__":
    main()
