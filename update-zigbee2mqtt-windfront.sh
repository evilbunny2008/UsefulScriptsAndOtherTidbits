#!/usr/bin/env sh

set -e

export Z2M_API_URLS=zigbee2mqtt1.delungra.com/api,zigbee2mqtt2.delungra.com/api
export Z2M_API_NAMES=Backyard,House

URL="$1"
if [ -z "$URL" ]; then
  echo "Usage: $0 <url-to-tar.gz>"
  exit 2
fi

# temp names
TMP_ARCHIVE="/tmp/download.tar.gz"
TMP_DIR="$(mktemp -d /tmp/extract.XXXXXX)"

# download (curl preferred)
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$URL" -o "$TMP_ARCHIVE"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$TMP_ARCHIVE" "$URL"
else
  echo "curl or wget required"
  exit 3
fi

# extract
#tar -xzf "$TMP_ARCHIVE" -C "$TMP_DIR"
unzip -qo "$TMP_ARCHIVE" -d "$TMP_DIR"

# copy extracted files into current directory (merge)
# find top-level entries in tmp dir and copy their contents
for entry in "$TMP_DIR"/*; do
  # if it's a directory, copy its contents; if file, copy file
  if [ -d "$entry" ]; then
    cp -a "$entry"/. .
  else
    cp -a "$entry" .
  fi
done

# cleanup download and temp dir
rm -f "$TMP_ARCHIVE"
rm -rf "$TMP_DIR"

# find the file with the template envs (adjusted to local path)
envs=$(ls -t ./assets/envs*.js | head -n1)

envsubst < "$envs" > ./envs_temp
cp ./envs_temp "$envs"
rm ./envs_temp
