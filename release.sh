#!/bin/sh

TAG="$1"
if [ -z "$TAG" ]; then
    echo "Usage: $0 <tag> (e.g. v1.0.2)"
    exit 1
fi

# Prüfen, ob der Tag lokal oder remote bereits existiert
if git rev-parse "$TAG" >/dev/null 2>&1 || git ls-remote --tags origin | grep -q "refs/tags/$TAG$"; then
    echo "Tag $TAG existiert bereits."
    printf "Möchtest du den bestehenden Tag (d)eleten/neu setzen oder auf die nächste Patch-Version (b)umpen? [d/b/Abbruch]: "
    read -r choice
    
    case "$choice" in
        d|D)
            echo "Lösche Tag $TAG lokal und remote..."
            git tag -d "$TAG" 2>/dev/null || true
            git push origin --delete "$TAG" 2>/dev/null || true
            ;;
        b|B)
            # Extrahiert Prefix (z.B. "v1.0.") und erhöht die letzte Ziffer um 1
            prefix=$(echo "$TAG" | sed 's/[0-9]*$//')
            version=$(echo "$TAG" | grep -o '[0-9]*$')
            new_version=$((version + 1))
            TAG="${prefix}${new_version}"
            echo "Neuer automatischer Tag: $TAG"
            ;;
        *)
            echo "Abgebrochen."
            exit 0
            ;;
    esac
fi

echo "Erstelle und pushe Tag $TAG..."
git tag "$TAG"
git push origin "$TAG"
