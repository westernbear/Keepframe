#!/usr/bin/env bash
# Sign extension/ into keepframe/ae/static/keepframe.zxp using Adobe ZXPSignCmd under Wine (Docker).
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cfg=$HOME/.config/keepframe
out=$root/keepframe/ae/static
mkdir -p "$out"; mkdir -p -m 700 "$cfg"; chmod 700 "$cfg"

stage=$(mktemp -d); trap 'rm -rf "$stage"' EXIT
# Stage only runtime paths; ZXPSignCmd writes into its input dir.
cp -r "$root/extension/"{CSXS,css,host,js,index.html} "$stage/"
find "$stage" \( -name '.*' -o -name '*~' -o -name '*.sw[op]' -o -name 'Thumbs.db' -o -iname 'desktop.ini' \) \
  -prune -exec rm -rf {} +

docker build -q -t keepframe-zxpsign "$root/scripts/zxp" >/dev/null
# Run ZXPSignCmd in the container. Output goes to $log; the command line (which holds the password) is never printed.
log=$(mktemp)
zxp() { docker run --rm -u "$(id -u):$(id -g)" --tmpfs "/tmp:uid=$(id -u),gid=$(id -g)" -e HOME=/tmp -e XDG_RUNTIME_DIR=/tmp \
  -v "$stage:/ext" -v "$out:/out" -v "$cfg:/cfg" keepframe-zxpsign "$@" >"$log" 2>&1; }
# step <name> <zxpsigncmd args...>: on failure print the step name and its output, then exit.
step() { local n=$1; shift; zxp "$@" || { echo "FAILED: $n" >&2; grep -v -e '^error: XDG_RUNTIME_DIR' -e '^wine: created' "$log" >&2 || true; exit 1; }; }

if [ ! -f "$cfg/zxp-cert.p12" ] || [ ! -f "$cfg/zxp-cert.pass" ]; then
  umask 077
  head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$cfg/zxp-cert.pass"
  step "create certificate" -selfSignedCert US CA Keepframe Keepframe "$(cat "$cfg/zxp-cert.pass")" /cfg/zxp-cert.p12 -validityDays 3650
  echo "created self-signed cert in $cfg"
fi
chmod 600 "$cfg/zxp-cert.p12" "$cfg/zxp-cert.pass"
pass=$(cat "$cfg/zxp-cert.pass")

# Cert expiry: never regenerate silently (a new cert changes the publisher identity).
certpem() { openssl pkcs12 -in "$cfg/zxp-cert.p12" -passin "pass:$pass" -nokeys -clcerts -legacy 2>/dev/null \
  || openssl pkcs12 -in "$cfg/zxp-cert.p12" -passin "pass:$pass" -nokeys -clcerts 2>/dev/null; }
if ! command -v openssl >/dev/null; then
  echo "note: openssl not found; skipping the cert expiry check"
elif ! pem=$(certpem) || [ -z "$pem" ]; then
  echo "note: could not read the cert with openssl; skipping the expiry check"
elif ! openssl x509 -noout -checkend $((30*86400)) <<<"$pem" >/dev/null; then
  echo "ERROR: signing cert expired or expires within 30 days ($(openssl x509 -noout -enddate <<<"$pem"))." >&2
  echo "Delete $cfg/zxp-cert.* and rerun to create a new one (this changes the publisher identity)." >&2
  exit 1
fi

rm -f "$out/keepframe.zxp"
if ! zxp -sign /ext /out/keepframe.zxp /cfg/zxp-cert.p12 "$pass" -tsa http://timestamp.digicert.com || [ ! -s "$out/keepframe.zxp" ]; then
  echo "note: timestamp server unavailable under this signer; signed without a timestamp"
  rm -f "$out/keepframe.zxp"
  step "sign" -sign /ext /out/keepframe.zxp /cfg/zxp-cert.p12 "$pass"
fi
step "verify" -verify /out/keepframe.zxp
grep -q 'Signature verified successfully' "$log" || { echo "FAILED: verify" >&2; cat "$log" >&2; exit 1; }
cat "$log" | grep -v '^wine: created' | grep -v '^error: XDG' || true
unzip -Z1 "$out/keepframe.zxp" >"$log" || { echo "FAILED: read ZXP contents" >&2; exit 1; }
if grep -Eq '(^|[/\\])test[/\\]' "$log"; then
  echo "FAILED: built ZXP contains test/ entries:" >&2
  grep -E '(^|[/\\])test[/\\]' "$log" >&2
  exit 1
fi
echo "built $out/keepframe.zxp"
