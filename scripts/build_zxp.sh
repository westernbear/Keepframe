#!/usr/bin/env bash
# Sign extension/ into keepframe/ae/static/keepframe.zxp using Adobe ZXPSignCmd under Wine (Docker).
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cfg=$HOME/.config/keepframe
out=$root/keepframe/ae/static
mkdir -p "$out"; mkdir -p -m 700 "$cfg"; chmod 700 "$cfg"

stage=$(mktemp -d); trap 'rm -rf "$stage"' EXIT
cp -r "$root/extension/." "$stage"  # ZXPSignCmd writes into its input dir

docker build -q -t keepframe-zxpsign "$root/scripts/zxp" >/dev/null
zxp() { docker run --rm -u "$(id -u):$(id -g)" --tmpfs "/tmp:uid=$(id -u),gid=$(id -g)" -e HOME=/tmp -e XDG_RUNTIME_DIR=/tmp \
  -v "$stage:/ext" -v "$out:/out" -v "$cfg:/cfg" keepframe-zxpsign "$@" 2>&1 | { grep -v -e '^error: XDG_RUNTIME_DIR' -e '^wine: created' || true; }; }

if [ ! -f "$cfg/zxp-cert.p12" ] || [ ! -f "$cfg/zxp-cert.pass" ]; then
  umask 077
  head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$cfg/zxp-cert.pass"
  zxp -selfSignedCert US CA Keepframe Keepframe "$(cat "$cfg/zxp-cert.pass")" /cfg/zxp-cert.p12 >/dev/null
  echo "created self-signed cert in $cfg"
fi
chmod 600 "$cfg/zxp-cert.p12" "$cfg/zxp-cert.pass"
pass=$(cat "$cfg/zxp-cert.pass")

rm -f "$out/keepframe.zxp"
# No -tsa: ZXPSignCmd crashes under Wine when it contacts a timestamp server (checked with digicert).
zxp -sign /ext /out/keepframe.zxp /cfg/zxp-cert.p12 "$pass" >/dev/null
zxp -verify /out/keepframe.zxp | tee /dev/stderr | grep -q 'Signature verified successfully' \
  || { echo "verify FAILED" >&2; exit 1; }
echo "built $out/keepframe.zxp"
