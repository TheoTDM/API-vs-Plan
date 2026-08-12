#!/bin/sh
# Installer for avp (API-vs-Plan).
#
#   curl -fsSL https://raw.githubusercontent.com/TheoTDM/API-vs-Plan/main/install.sh | sh
#
# Downloads the source tree and links `avp` into a directory on your PATH.
# Nothing is installed into your Python environment -- the tool is pure stdlib,
# and Homebrew's Python is PEP 668 EXTERNALLY-MANAGED so pip would refuse anyway.
#
# This script does NOT modify your shell configuration. If the bin directory is
# not on your PATH it prints the line to add and leaves the decision to you.
#
# Environment overrides:
#   AVP_REF     branch, tag or commit to install        (default: main)
#   AVP_PREFIX  where the source tree lives             (default: ~/.local/share)
#   AVP_BINDIR  where the `avp` symlink goes            (default: ~/.local/bin)
#   AVP_FORCE   set to 1 to replace an existing `avp`   (default: unset)
#
# Uninstall:  rm -rf ~/.local/share/avp ~/.local/bin/avp

set -eu

REPO="TheoTDM/API-vs-Plan"
REF="${AVP_REF:-main}"
PREFIX="${AVP_PREFIX:-${XDG_DATA_HOME:-$HOME/.local/share}}"
BINDIR="${AVP_BINDIR:-$HOME/.local/bin}"
DEST="$PREFIX/avp"
TARBALL="https://codeload.github.com/$REPO/tar.gz/$REF"

die() {
    echo "install: $*" >&2
    exit 1
}

# --- preflight ------------------------------------------------------------
for tool in curl tar; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required but not installed"
done

# --- download into scratch space -----------------------------------------
TMP=$(mktemp -d) || die "could not create a temporary directory"
# Clean up the scratch dir however we exit, including on failure.
trap 'rm -rf "$TMP"' EXIT INT TERM

echo "downloading $REPO@$REF"
mkdir -p "$TMP/src"
# Download and extract as separate steps rather than `curl | tar`. In POSIX sh a
# pipeline's exit status is the last command's, so piping would let a failed
# download be reported as a malformed archive (`pipefail` is not POSIX).
# -f makes an HTTP error a failure instead of writing an error page to disk.
curl -fsSL "$TARBALL" -o "$TMP/avp.tar.gz" \
    || die "could not download $REPO@$REF -- is AVP_REF=$REF a real branch, tag or commit?"
tar -xzf "$TMP/avp.tar.gz" --strip-components=1 -C "$TMP/src" \
    || die "downloaded archive could not be extracted"

# --- validate before touching anything already installed ------------------
# A truncated or wrong-shaped download must never reach the install directory.
[ -f "$TMP/src/bin/avp" ] || die "downloaded archive is missing bin/avp"
[ -f "$TMP/src/src/avp/cli.py" ] || die "downloaded archive is missing src/avp/cli.py"

# The exec bit does survive the GitHub tarball, but a restrictive umask or an
# unusual tar shouldn't be able to produce a non-executable command.
chmod +x "$TMP/src/bin/avp"

# --- swap into place ------------------------------------------------------
# Stage beside the destination, then move: a failed update leaves the previous
# install intact rather than a half-written tree.
mkdir -p "$PREFIX"
rm -rf "$DEST.new" "$DEST.old"
mv "$TMP/src" "$DEST.new"
if [ -e "$DEST" ]; then
    mv "$DEST" "$DEST.old"
fi
mv "$DEST.new" "$DEST"
rm -rf "$DEST.old"

# --- link onto PATH -------------------------------------------------------
mkdir -p "$BINDIR"
LINK="$BINDIR/avp"
if [ -e "$LINK" ] || [ -L "$LINK" ]; then
    current=$(readlink "$LINK" 2>/dev/null || echo "$LINK")
    case "$current" in
        "$DEST"/*) ;;  # ours already, safe to refresh
        *)
            if [ "${AVP_FORCE:-}" != "1" ]; then
                echo "install: $LINK already exists and points at:" >&2
                echo "           $current" >&2
                echo "         This looks like a different install (a development" >&2
                echo "         checkout, perhaps). Refusing to replace it." >&2
                echo "         Re-run with AVP_FORCE=1 to override, or set" >&2
                echo "         AVP_BINDIR to install somewhere else." >&2
                exit 1
            fi
            ;;
    esac
fi
ln -sf "$DEST/bin/avp" "$LINK"

# --- smoke test -----------------------------------------------------------
# Never report success for an install that cannot actually run.
"$LINK" --help >/dev/null 2>&1 || die "installed avp but it failed to run"

# --- report ---------------------------------------------------------------
echo
echo "installed avp ($REF)"
echo "  source: $DEST"
echo "  binary: $LINK"
echo

case ":$PATH:" in
    *":$BINDIR:"*)
        echo "Run it:"
        echo "  avp --plan max-5x"
        ;;
    *)
        # Print the line; deliberately do not edit any dotfile.
        case "${SHELL:-}" in
            *zsh) rc="~/.zshrc" ;;
            *bash) rc="~/.bashrc" ;;
            *) rc="your shell profile" ;;
        esac
        echo "$BINDIR is not on your PATH. Add it to $rc:"
        echo
        echo "  export PATH=\"$BINDIR:\$PATH\""
        echo
        echo "Or run it directly right now:"
        echo "  $LINK --plan max-5x"
        ;;
esac
