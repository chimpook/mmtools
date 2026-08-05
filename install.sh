#!/usr/bin/env bash
# Install the /mm skill for the current user by symlinking it, so edits in this
# repo take effect without re-copying. Freeplane's add-on is installed separately
# through its own UI - see README.md.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.claude/skills/mm"

mkdir -p "$HOME/.claude/skills"
if [ -L "$DEST" ]; then
    rm "$DEST"
elif [ -e "$DEST" ]; then
    echo "refusing to replace $DEST - it exists and is not a symlink" >&2
    exit 1
fi
ln -s "$HERE/skills/mm" "$DEST"
echo "linked $DEST -> $HERE/skills/mm"
echo
echo "Claude Code loads skills at startup, so restart it before /mm appears."
