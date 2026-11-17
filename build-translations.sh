#!/usr/bin/env bash
# Build the translation catalogues for the network monitor.
#
# The .ts files are committed untranslated on purpose: the language plumbing is
# scaffolded, but 0.4.0 ships no translation. To translate, open
# translations/network-process_<locale>.ts, fill in the <translation> elements
# and run this script; the .qm files it writes beside them are what the app
# loads at startup.
#
#   ./build-translations.sh            # every locale with a .ts file
#   ./build-translations.sh da         # just one
#
# Requires pylupdate6 (pyside6-tools) and lrelease6 (qt6-tools). The .qm files
# are build artefacts and stay untracked; the .ts files are the source.
set -euo pipefail
cd "$(dirname "$0")"

for tool in pylupdate6 lrelease6; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "error: $tool not found (pyside6-tools / qt6-tools)" >&2
        exit 1
    fi
done

locales=("$@")
if [ ${#locales[@]} -eq 0 ]; then
    locales=()
    for ts in translations/network-process_*.ts; do
        [ -e "$ts" ] || continue
        name=${ts##*/network-process_}
        locales+=("${name%.ts}")
    done
fi

for locale in "${locales[@]}"; do
    ts="translations/network-process_${locale}.ts"
    qm="translations/network-process_${locale}.qm"
    if [ ! -f "$ts" ]; then
        echo "error: $ts does not exist" >&2
        exit 1
    fi
    # Refresh the source strings without dropping translations already filled in.
    pylupdate6 network-process.py -ts "$ts" --no-obsolete
    lrelease6 "$ts" -qm "$qm"
    echo "built $qm"
done
