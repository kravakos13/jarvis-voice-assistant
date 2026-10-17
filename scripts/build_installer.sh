#!/usr/bin/env bash
# Build build/out/JarvisSetup.exe with Inno Setup (natively on Windows or under Wine on Linux).
#   PY_EMBED=python-3.11.9-embed-amd64.zip  [ISCC="wine C:/InnoSetup/ISCC.exe"]  scripts/build_installer.sh
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
PY_EMBED=${PY_EMBED:-$ROOT/../python-3.11.9-embed-amd64.zip}
if [ -n "${ISCC:-}" ]; then read -r -a ISCC_CMD <<< "$ISCC"; else ISCC_CMD=(wine "C:/Program Files (x86)/Inno Setup 6/ISCC.exe"); fi
VER=$(python3 -c "import re;print(re.search(r'__version__\s*=\s*\"([^\"]+)',open('jarvis/__init__.py').read()).group(1))")
[ -f installer/get-pip.py ] || curl -fsSL -o installer/get-pip.py https://bootstrap.pypa.io/get-pip.py
STAGE=build/stage
rm -rf build && mkdir -p "$STAGE/python"
# app files (git-tracked layout; no user data, models, logs, secrets)
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.*' --exclude='build' --exclude='logs' \
    --exclude='voices/*.onnx*' --exclude='settings.local.yaml' --exclude='*.session*' --exclude='tests' \
    --exclude='cache' -cf - jarvis scripts installer assets voices firmware config.yaml aliases.yaml devices.yaml \
    requirements*.txt Jarvis.vbs README.md CHANGELOG.md | tar -xf - -C "$STAGE"
# embeddable Python with site-packages + app root on sys.path
unzip -q "$PY_EMBED" -d "$STAGE/python"
cat > "$STAGE/python/python311._pth" <<'PTH'
python311.zip
.
..
Lib\site-packages
import site
PTH
mkdir -p "$STAGE/python/Lib/site-packages"
"${ISCC_CMD[@]}" /Qp "/DAppVersion=$VER" installer/jarvis.iss
ls -la build/out/JarvisSetup.exe
