#!/bin/bash
# install.sh — install the tango.me extractor as a yt-dlp plugin.
#
# yt-dlp discovers plugins under <config>/yt-dlp/plugins/<name>/yt_dlp_plugins/.
# We symlink this checkout in, so edits here take effect immediately.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
YTDLP="${YTDLP:-$(command -v yt-dlp || true)}"

if [ -z "$YTDLP" ]; then
    echo "error: yt-dlp not found in PATH" >&2
    echo "       install it (brew install yt-dlp / pipx install yt-dlp) or set YTDLP=/path/to/yt-dlp" >&2
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "warning: ffmpeg not found; live recording needs it" >&2
fi

PLUGIN_ROOT="${XDG_CONFIG_HOME:-$HOME/.config}/yt-dlp/plugins"
DEST="$PLUGIN_ROOT/tango-yt-dlp"
mkdir -p "$DEST"
ln -sfn "$SCRIPT_DIR/yt_dlp_plugins" "$DEST/yt_dlp_plugins"

echo "==> linked $DEST/yt_dlp_plugins -> $SCRIPT_DIR/yt_dlp_plugins"
echo "==> yt-dlp: $YTDLP ($("$YTDLP" --version))"

# Verify discovery only; --flat-playlist keeps it to one request.  grep must
# read all input (no -q), otherwise yt-dlp dies of SIGPIPE and pipefail fails
# the check.
if "$YTDLP" --verbose --simulate --ignore-errors --flat-playlist \
        "https://www.tango.me/live" 2>&1 | grep 'Extractor Plugins:.*Tango'; then
    echo "==> plugin loaded"
else
    echo "warning: plugin did not show up in yt-dlp's plugin list" >&2
    exit 1
fi

cat <<'EOF'

===== tango.me yt-dlp plugin installed =====

  Record a stream:
    yt-dlp -f 1 "https://www.tango.me/stream/<id>"

  Record a live stream by profile alias:
    yt-dlp -f 1 "https://www.tango.me/<alias>"

  List everyone who is live right now:
    yt-dlp --flat-playlist "https://www.tango.me/live"

  Private / followers-only streams (reuses your browser login):
    yt-dlp -f 1 --cookies-from-browser chrome "https://www.tango.me/<alias>"

  Wait for an offline profile to start streaming:
    yt-dlp --wait-for-video 60 "https://www.tango.me/<alias>"

  Stop a recording with Ctrl-C; the .mp4 is finalised.
EOF
