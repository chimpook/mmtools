#!/usr/bin/env bash
# Install the /mm skill and the two commands for the current user by symlinking
# them, so edits in this repo take effect without re-copying. Freeplane's add-on
# is installed separately through its own UI - see README.md.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Symlink $2 to $1, replacing an existing symlink but never a real file.
link() {
    if [ -L "$2" ]; then
        rm "$2"
    elif [ -e "$2" ]; then
        echo "refusing to replace $2 - it exists and is not a symlink" >&2
        exit 1
    fi
    ln -s "$1" "$2"
    echo "linked $2 -> $1"
}

mkdir -p "$HOME/.claude/skills"
link "$HERE/skills/mm" "$HOME/.claude/skills/mm"

# Only the two commands go on PATH, not the whole repo. ~/.local/bin is on PATH
# by default on most distributions; if it is not, add it in your shell rc.
mkdir -p "$HOME/.local/bin"
link "$HERE/mm.py" "$HOME/.local/bin/mm.py"
link "$HERE/mmd.py" "$HOME/.local/bin/mmd.py"

case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "note: $HOME/.local/bin is not on PATH - add it to your shell rc" >&2 ;;
esac

echo
echo "Set MM_HOME to your default map collection so aliases resolve from any"
echo "directory, e.g. in ~/.bashrc:  export MM_HOME=\"\$HOME/mindmaps\""
echo
echo "Claude Code loads skills at startup, so restart it before /mm appears."
