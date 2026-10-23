#!/usr/bin/env bash
# Clone OpenShorts into services/openshorts WITHOUT its cloud/ folder.
# cloud/ has a separate commercial license, so we never check it out or use it.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="${1:-services/openshorts}"
if [ -d "$DIR/.git" ]; then
  echo "OpenShorts already in $DIR; updating"
  git -C "$DIR" pull --ff-only
else
  mkdir -p "$(dirname "$DIR")"
  git clone --depth 1 --filter=blob:none --sparse https://github.com/mutonby/openshorts "$DIR"
fi
git -C "$DIR" sparse-checkout set --no-cone '/*' '!/cloud/'
if [ -e "$DIR/cloud" ]; then
  echo "ERROR: cloud/ was checked out; remove it before running anything" >&2
  exit 1
fi
echo "OpenShorts ready in $DIR (cloud/ excluded). Start it with: cinekahani shorts up"
