#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 || ! $1 =~ ^[0-9]+\.[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
    echo 'Usage: build-plugin.sh VERSION OUTPUT_DIRECTORY' >&2
    exit 2
fi
version=$1
output=$2
root=$(dirname "$(dirname "$(realpath "$0")")")
template="$root/plugin/NerdTools.plg"
source_dir="$root/source/NerdTools"
package="NerdTools-$version-x86_64-1.txz"
if ! grep -q '@VERSION@' "$template" || ! grep -q '@SHA256@' "$template"; then
    echo 'Plugin template must contain @VERSION@ and @SHA256@' >&2
    exit 1
fi
mkdir -p -- "$output"
output=$(realpath "$output")
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
mkdir -p "$work/root"
cp -R "$source_dir/usr" "$work/root/"
cp "$root/LICENSE" "$work/root/usr/local/emhttp/plugins/NerdTools/LICENSE"
if command -v gtar >/dev/null 2>&1; then
    tar_bin=gtar
else
    tar_bin=tar
fi
"$tar_bin" --sort=name --owner=0 --group=0 --numeric-owner \
    --mtime="@${SOURCE_DATE_EPOCH:-0}" -C "$work/root" -cJf "$work/$package" .
sha=$(sha256sum "$work/$package")
sha=${sha%% *}
sed -e "s/@VERSION@/$version/g" -e "s/@SHA256@/$sha/g" "$template" > "$work/NerdTools.plg"
mv -- "$work/$package" "$work/NerdTools.plg" "$output/"
