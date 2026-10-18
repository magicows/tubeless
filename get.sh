#!/bin/sh
# One-line installer. Downloads the latest Tubeless release, checks its SHA-256, and runs install.sh with your options.
#
#     curl -fsSL https://raw.githubusercontent.com/magicows/tubeless/main/get.sh | sudo sh -s -- --name "Living Room"
#
# Everything after `--` goes to install.sh (try --dry-run first). TUBELESS_VERSION=0.1.0 picks a release instead of the latest.
# Running it again updates the code. Your videos and settings are never touched.
set -eu
REPO="magicows/tubeless"
say() { printf '%s\n' "$*"; }
die() { printf 'get.sh: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run it with sudo (it installs packages and services)"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"

VERSION="${TUBELESS_VERSION:-}"
if [ -z "$VERSION" ]; then
  say "Finding the latest release..."
  url=$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest") || die "could not reach GitHub"
  VERSION="${url##*/v}"
  case "$VERSION" in *[!0-9.]*|"") die "there is no release yet at https://github.com/$REPO/releases" ;; esac
fi
NAME="tubeless-$VERSION"
BASE="https://github.com/$REPO/releases/download/v$VERSION"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"
say "Downloading Tubeless $VERSION..."
curl -fsSL -o "$NAME.tar.gz" "$BASE/$NAME.tar.gz" || die "could not download $BASE/$NAME.tar.gz"
curl -fsSL -o "$NAME.tar.gz.sha256" "$BASE/$NAME.tar.gz.sha256" || die "could not download the checksum"
sha256sum -c "$NAME.tar.gz.sha256" >/dev/null 2>&1 || die "the download does not match its checksum; nothing was installed"
say "Checksum OK."
tar -xzf "$NAME.tar.gz"
[ -f "$NAME/install.sh" ] || die "the release has no install.sh"
cd "$NAME"
status=0
sh install.sh "$@" || status=$?              # not exec: the cleanup above has to run afterwards
exit "$status"
