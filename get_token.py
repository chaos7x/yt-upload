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

from datetime import datetime, timezone, timedelta
import json
import os
import sys

# WICHTIG: Muss ganz oben stehen, BEVOR google_auth_oauthlib importiert wird!
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_SECRETS_FILE = "/app/oauth/client_secrets.json"
OUTPUT_CREDENTIALS_FILE = "/app/oauth/youtube-upload-credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube"
]

def main():
    try:
        # Zielverzeichnis automatisch anlegen, falls es noch nicht existiert
        os.makedirs(os.path.dirname(OUTPUT_CREDENTIALS_FILE), exist_ok=True)

        # Wir nutzen hier den Konsolen-Flow (run_local_server wird umgangen)
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS_FILE, scopes=SCOPES)
        
        # redirect_uri auf localhost setzen, aber wir holen den Code manuell aus der URL!
        flow.redirect_uri = "http://localhost:8080/"
        
        auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')
        
        print("\n1. Öffne diesen Link im Browser:\n")
        print(auth_url)
        print("\n----------------------------------------------------------------")
        print("2. Logge dich ein und erlaube den Zugriff.")
        print("3. Nach dem Klick auf 'Zulassen' bricht der Browser ab (Seite nicht gefunden).")
        print("4. Kopiere die KOMPLETTE URL aus der Adresszeile des Browsers (beginnt mit http://localhost:8080/?code=...)")
        print("----------------------------------------------------------------\n")
        
        redirect_response = input("Füge die kopierte URL hier ein: ").strip()
        
        # Holt sich den Token aus der URL heraus
        flow.fetch_token(authorization_response=redirect_response)
        credentials = flow.credentials
        
        # Datumsformatierung mit Fallback, falls credentials.expiry None ist
        if credentials.expiry:
            expiry_str = credentials.expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            expiry_str = (datetime.now(timezone.utc) + timedelta(seconds=3600)).strftime("%Y-%m-%dT%H:%M:%SZ")

        legacy_credentials = {
            "access_token": credentials.token,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "refresh_token": credentials.refresh_token,
            "token_expiry": expiry_str,
            "token_uri": credentials.token_uri,
            "user_agent": None,
            "revoke_uri": "https://oauth2.googleapis.com/revoke",
            "id_token": None,
            "id_token_jwt": None,
            "token_response": {
                "access_token": credentials.token,
                "expires_in": 3599,
                "refresh_token": credentials.refresh_token,
                "scope": " ".join(SCOPES),
                "token_type": "Bearer"
            },
            "scopes": SCOPES,
            "token_info_uri": "https://oauth2.googleapis.com/tokeninfo",
            "invalid": False,
            "_class": "OAuth2Credentials",
            "_module": "oauth2client.client"
        }
        
        with open(OUTPUT_CREDENTIALS_FILE, "w") as f:
            json.dump(legacy_credentials, f, indent=2)
            
        print(f"\n[ERFOLG] {OUTPUT_CREDENTIALS_FILE} wurde erfolgreich erstellt!")

    except KeyboardInterrupt:
        print("\n\n[ABBRUCH] Vorgang durch Benutzer abgebrochen (Strg + C). Es wurde nichts gespeichert.")
        sys.exit(130)

if __name__ == "__main__":
    main()
