#!/bin/sh
# Installs the-one-server, The One on a Linux server with no screen (roadmap F8;
# docs/architecture/linux-headless.md). Published beside the archives as install.sh. The archive
# brings its own Python and Node; the server's data and credentials (~/.local/share/the-one-server,
# ~/.config/the-one-server) are left alone, and an upgrade keeps the version before it. A machine
# with musl instead of glibc (Alpine) gets the archive built for musl, named ...-<arch>-musl.
set -eu

DEFAULT_BASE_URL="https://updates.the-zeroth.com/server/linux"
GLIBC_FLOOR="2.28"
MUSL_FLOOR="1.2.2"

say() { printf '%s\n' "$*" >&2; }

usage() {
    cat >&2 <<'USAGE'
Install the-one-server, The One on a Linux server with no screen.

  curl -fsSL https://updates.the-zeroth.com/server/linux/install.sh | sh
  wget -qO- https://updates.the-zeroth.com/server/linux/install.sh | sh     # Alpine has no curl
  sh install.sh --archive the-one-server-<version>-linux-<arch>[-musl].tar.gz

Options:
  --archive FILE   install this archive instead of downloading one; checked against
                   FILE.sha256 when that file is beside it
  --version V      the version to download (default: the latest published)
  --base-url URL   where versions are published (default: $THE_ONE_SERVER_BASE_URL, else
                   https://updates.the-zeroth.com/server/linux)
  --prefix DIR     where versions are kept (default: ~/.local/lib/the-one-server;
                   as root, /opt/the-one-server)
  --bin-dir DIR    where the command goes (default: ~/.local/bin; as root, /usr/local/bin)

Needs Linux on x86_64 or arm64 with glibc 2.28 or later (Debian 10, Ubuntu 20.04, RHEL 8 and
later) or musl 1.2.2 or later (Alpine 3.13 and later), tar, gzip and sha256sum, and curl or wget
to download.
USAGE
}
fail() { say "the-one-server install: $*"; exit 1; }

archive=""
version=""
base_url="${THE_ONE_SERVER_BASE_URL:-$DEFAULT_BASE_URL}"
if [ "$(id -u)" = 0 ]; then
    prefix="/opt/the-one-server"
    bin_dir="/usr/local/bin"
else
    prefix="${HOME:?HOME is not set}/.local/lib/the-one-server"
    bin_dir="$HOME/.local/bin"
fi

while [ $# -gt 0 ]; do
    case $1 in
        --archive|--version|--base-url|--prefix|--bin-dir)
            [ $# -ge 2 ] || fail "$1 needs a value"
            option=$1 value=$2
            shift 2
            ;;
        --archive=*|--version=*|--base-url=*|--prefix=*|--bin-dir=*)
            option=${1%%=*} value=${1#*=}
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option $1 (see --help)"
            ;;
    esac
    case $option in
        --archive) archive=$value ;;
        --version) version=$value ;;
        --base-url) base_url=${value%/} ;;
        --prefix) prefix=$value ;;
        --bin-dir) bin_dir=$value ;;
    esac
done

# --- this machine ---------------------------------------------------------------------------

[ "$(uname -s)" = Linux ] || fail "the-one-server runs on Linux; this is $(uname -s)."
case $(uname -m) in
    x86_64|amd64) arch=x86_64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) fail "there is no build for $(uname -m); there are builds for x86_64 and arm64." ;;
esac

glibc_version() {
    found=$(getconf GNU_LIBC_VERSION 2>/dev/null | sed -n 's/^glibc \([0-9][0-9.]*\)$/\1/p')
    if [ -z "$found" ] && command -v ldd >/dev/null 2>&1; then
        found=$(ldd --version 2>&1 | sed -n '1s/^ldd .* \([0-9][0-9]*\.[0-9][0-9]*\)$/\1/p')
    fi
    printf '%s' "$found"
}

# musl's dynamic loader says which musl it is ("Version 1.2.4") and exits 1, as Alpine's ldd
# (the same program) or run by itself.
musl_version() {
    case $arch in
        x86_64) loader=/lib/ld-musl-x86_64.so.1 ;;
        arm64) loader=/lib/ld-musl-aarch64.so.1 ;;
    esac
    banner=""
    if command -v ldd >/dev/null 2>&1; then
        banner=$(ldd --version 2>&1 || true)
    fi
    case $banner in
        *"musl libc"*) ;;
        *) banner=$( [ -x "$loader" ] && "$loader" 2>&1 || true) ;;
    esac
    printf '%s\n' "$banner" | sed -n 's/^Version \([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\).*$/\1/p'
}

# Whether version $1 is $2 or later, part by part (a missing part counts as 0).
at_least() {
    have=$1 need=$2
    for part in 1 2 3; do
        have_part=${have%%.*} need_part=${need%%.*}
        [ "${have_part:-0}" -gt "${need_part:-0}" ] && return 0
        [ "${have_part:-0}" -lt "${need_part:-0}" ] && return 1
        case $have in *.*) have=${have#*.} ;; *) have="" ;; esac
        case $need in *.*) need=${need#*.} ;; *) need="" ;; esac
    done
    return 0
}

glibc=$(glibc_version)
if [ -n "$glibc" ]; then
    at_least "$glibc" "$GLIBC_FLOOR" || fail "this system has glibc $glibc; the-one-server needs \
$GLIBC_FLOOR or later (Debian 10, Ubuntu 20.04, RHEL 8 and later)."
    platform=$arch
else
    musl=$(musl_version)
    [ -n "$musl" ] || fail "this system has neither glibc nor musl; the-one-server needs glibc \
$GLIBC_FLOOR or later (Debian 10, Ubuntu 20.04, RHEL 8 and later) or musl $MUSL_FLOOR or later \
(Alpine 3.13 and later)."
    at_least "$musl" "$MUSL_FLOOR" || fail "this system has musl $musl; the-one-server needs \
$MUSL_FLOOR or later (Alpine 3.13 and later)."
    platform=$arch-musl
fi
for tool in tar gzip sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || fail "needs $tool, which this system does not have."
done

# --- the archive ----------------------------------------------------------------------------

work=$(mktemp -d "${TMPDIR:-/tmp}/the-one-server-install.XXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT TERM

# fetch URL FILE [why it may be missing]
fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 -o "$2" "$1" || fail "could not download $1${3:+; $3}"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1" || fail "could not download $1${3:+; $3}"
    else
        fail "needs curl or wget to download, or pass --archive FILE."
    fi
}

check_digest() {
    expected=$(sed -n '1s/^\([0-9a-f]\{64\}\).*/\1/p' "$2")
    [ -n "$expected" ] || fail "$2 holds no sha256."
    actual=$(sha256sum "$1" | sed 's/ .*//')
    [ "$actual" = "$expected" ] || fail "$(basename "$1") does not match its sha256 (got $actual, \
expected $expected); download it again."
}

if [ -n "$archive" ]; then
    [ -f "$archive" ] || fail "$archive is not a file."
    name=$(basename "$archive" .tar.gz)
    if [ -f "$archive.sha256" ]; then
        check_digest "$archive" "$archive.sha256"
    fi
else
    if [ -z "$version" ]; then
        fetch "$base_url/latest.txt" "$work/latest.txt"
        version=$(sed -n '1{s/[[:space:]]//g;p;}' "$work/latest.txt")
    fi
    case $version in
        ""|*[!0-9A-Za-z.+-]*) fail "\"$version\" is not a version." ;;
    esac
    name="the-one-server-$version-linux-$platform"
    archive="$work/$name.tar.gz"
    missing=""
    [ "$platform" = "$arch" ] || missing="0.5.0 and earlier have no build for musl"
    say "Downloading $base_url/$version/$name.tar.gz"
    fetch "$base_url/$version/$name.tar.gz" "$archive" "$missing"
    fetch "$base_url/$version/$name.tar.gz.sha256" "$archive.sha256"
    check_digest "$archive" "$archive.sha256"
fi
case $name in
    the-one-server-*-linux-"$platform") ;;
    the-one-server-*-linux-*) fail "$(basename "$archive") is for ${name##*-linux-}; this machine \
is $platform." ;;
    *) fail "$(basename "$archive") is not a the-one-server archive." ;;
esac

mkdir -p "$prefix"
incoming=$(mktemp -d "$prefix/.incoming.XXXXXX")
trap 'rm -rf "$work" "$incoming"' EXIT
tar -xzf "$archive" -C "$incoming" || fail "could not unpack $(basename "$archive")."
unpacked="$incoming/$name"
[ -x "$unpacked/bin/the-one-server" ] && [ -f "$unpacked/VERSION" ] \
    || fail "$(basename "$archive") holds no $name/bin/the-one-server."
installed_version=$(sed -n '1p' "$unpacked/VERSION")
case $installed_version in
    ""|*[!0-9A-Za-z.+-]*) fail "$(basename "$archive") names no valid version." ;;
esac

# --- in place -------------------------------------------------------------------------------

previous=""
if [ -L "$prefix/current" ]; then
    previous=$(readlink "$prefix/current")
fi
rm -rf "${prefix:?}/${installed_version:?}"
mv "$unpacked" "$prefix/$installed_version"
ln -sfn "$installed_version" "$prefix/current"
mkdir -p "$bin_dir"
ln -sfn "$prefix/current/bin/the-one-server" "$bin_dir/the-one-server"

# Keep this version and the one before it (a server started from that one may still run).
for kept in "$prefix"/*/; do
    kept=${kept%/}
    [ ! -L "$kept" ] && [ -f "$kept/bundle.json" ] || continue
    case ${kept##*/} in
        "$installed_version"|"$previous") ;;
        *) rm -rf "$kept" ;;
    esac
done

shown=$("$bin_dir/the-one-server" --version 2>&1) \
    || fail "the installed command does not start: $shown"
say "Installed $shown in $prefix/$installed_version"
say "Command: $bin_dir/the-one-server"
node="$prefix/$installed_version/node/bin/node"
if [ "$platform" != "$arch" ] && [ -x "$node" ] && ! "$node" --version >/dev/null 2>&1; then
    # Node's musl build links the machine's C++ runtime, which Alpine leaves out by default.
    say "Its Node, which MCP servers written for Node run on, needs libstdc++ from GCC 13 or later:"
    say "apk add libstdc++ (Alpine 3.19 and later). The rest of the server runs without it."
fi
if ! command -v bash >/dev/null 2>&1; then
    say "This system has no bash, so Agents' commands run with sh, and those written for bash fail"
    say "until you install it (apk add bash on Alpine)."
fi
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) say "$bin_dir is not on your PATH; add it: export PATH=\"$bin_dir:\$PATH\"" ;;
esac
if [ -n "$previous" ] && [ "$previous" != "$installed_version" ]; then
    say "A server already running keeps $previous until you restart it: the-one-server stop, then start."
fi
say "Next: export ANTHROPIC_API_KEY=... (or write ~/.config/the-one-server/credentials.json, mode 600),"
say "then: the-one-server run \"your task\" --workdir DIR"
