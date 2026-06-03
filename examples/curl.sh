#!/usr/bin/env bash
# curl examples against codex-image-api.
set -euo pipefail
BASE="http://127.0.0.1:10532"

echo "== health =="
curl -s "$BASE/health"; echo

echo "== text -> image (b64) =="
curl -s -X POST "$BASE/v1/images/generations" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-image-2","prompt":"a tiny dragon on a stack of books","size":"1024x1024","quality":"low"}' \
  | python3 -c 'import sys,json,base64;d=json.load(sys.stdin);open("dragon.png","wb").write(base64.b64decode(d["data"][0]["b64_json"]));print("saved dragon.png")'

echo "== text -> image (url, n=2) =="
curl -s -X POST "$BASE/v1/images/generations" \
  -H "Content-Type: application/json" \
  -d '{"prompt":"a shiba inu wearing sunglasses, flat illustration","n":2,"response_format":"url","quality":"low"}'
echo

echo "== image -> image (edit) =="
curl -s -X POST "$BASE/v1/images/edits" \
  -F "image=@dragon.png;type=image/png" \
  -F "prompt=make it night, add glowing runes" \
  -F "quality=low" \
  | python3 -c 'import sys,json,base64;d=json.load(sys.stdin);open("dragon_night.png","wb").write(base64.b64decode(d["data"][0]["b64_json"]));print("saved dragon_night.png")'
