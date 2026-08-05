# mmtools — Freeplane mind maps as a Claude Code surface

Three features, deliberately separable:

1. **Read/write mind maps** — `mm.py`, a lossless Freeplane editor and library
2. **Session maps as an alternative UI** — the MM Watch add-on plus the `/mm` skill, so a
   running Claude Code session can be mirrored into a map and driven from it
3. **Sync session results into a long-lived map** — not built yet

None of this is specific to any map collection. A collection is just a directory with
`.mm` files and an `.mmrc`; `mm.py` finds the collection by walking up from the cwd.

> History note: this code was developed inside a private map collection and split out on
> 2026-08-05. Commits before that live in that repository, not here.

## Install

```bash
git clone <this repo> ~/apps/mmtools
~/apps/mmtools/install.sh    # symlinks the /mm skill and mm.py/mmd.py into place
```

`install.sh` symlinks the `/mm` skill into `~/.claude/skills/` and the two commands into
`~/.local/bin/`, so `mm.py` and `mmd.py` work from any directory and edits in this repo
take effect immediately. Add this to your shell rc so alias addressing works outside a
collection:

```bash
export MM_HOME="$HOME/mindmaps"      # your default map collection
```

Then, in Freeplane: **Tools > Add-ons…** → install from file →
`~/apps/mmtools/freeplane/mmwatch.addon.mm`, restart Freeplane, and arm it once per session
via **Tools > MM Watch > Start map watcher**.

## A map collection

Any directory holding `.mm` files, with an `.mmrc` giving them names:

```json
{
  "maps":       { "work": "MyMap.mm", "archive": "MyArchive.mm" },
  "style_from": "work",
  "sessions":   "sessions"
}
```

- `maps` — alias → filename, so you can write `work:some/path` or `work:ID_123`
- `style_from` — which map to lift the `MapStyle` hook from, so new session maps render
  icons and colours the same way
- `sessions` — where session maps go, relative to the collection root

Without an `.mmrc` there are no aliases and maps are addressed by path
(`SomeMap.mm:ID_123`), which always works.

**Which collection a command uses:** the nearest ancestor of the cwd holding an `.mmrc`;
failing that, `$MM_HOME`; failing that, this repo. So a collection you have cd'd into
always wins, and `MM_HOME` only decides what "no prefix" means when you are outside every
collection — which is what makes the aliases usable from an arbitrary directory.

## Feature 1 — `mm.py`

```
mm.py tree [target] [-d N]              structure with subtree sizes
mm.py find <regex> [-i ICON] [-m MAP]   search node text; prints ids
mm.py show <target>                     text, note, icons, dates, children
mm.py add <parent> <text> [--icon X] [--note T] [--link U] [--first]
mm.py icon <target> --set done | --add waiting | --clear
mm.py note <target> <text>
mm.py rename <target> <text>
mm.py rm <target> --yes
mm.py mv <target> --to <parent>         works across maps
mm.py post <mapfile> <target>           enqueue a nested node spec (JSON on stdin)
mm.py inbox <mapfile> [--done ID...]    pending launch-marked nodes
mm.py session new|list · watch · icons · stats · check
```

**Every write is a byte-level splice into the original file.** Untouched parts of the map
are never re-serialized, so styles, attribute order, formatting and rich text survive
exactly as Freeplane wrote them. Do not reach for `ElementTree` to write a map — it
reorders attributes and churns the whole file into an unreadable diff.

**Addressing:** `alias:path/of/text`, `alias:ID_123`, `SomeMap.mm:ID_123`, a bare
`ID_123` (searched across the configured maps), or `alias:` for a map root. Node text
often contains `/` and `::`, so text paths only resolve for shallow, clean names — get an
id from `find` for anything deeper.

## Feature 2 — the bridge

```
mm.py --live …                 append a command instead of writing the file
        ↓
~/.mm-bridge/queue.jsonl       user-level, shared by every collection
        ↓
MM Watch add-on (in Freeplane) applies it to the OPEN, in-memory map
```

Freeplane stays the only process that writes a `.mm` file, which removes the
write-conflict problem rather than managing it. Edits arrive as **unsaved changes** — the
watcher never saves, so undo keeps working and you decide when to write out.

The bridge path is a constant, not config: queue commands carry the absolute path of the
map they target, so one queue serves every collection and the Groovy watcher needs no
configuration at all. `MM_BRIDGE_DIR` relocates it for testing.

`docs/ADDON.md` covers the add-on itself — architecture, the rebuild loop, permissions,
troubleshooting, and why Freeplane's signed-script mechanism cannot be used here.
`docs/BRIDGE.md` covers `mmd.py` and the session-map protocol, with the security
measurements — **read it before widening any permission.** The short version: pattern
scoping is *not* enforced in `claude -p`, so the session-map path runs read-only.

## Feature 3 — sync into a long-lived map

Not built. The intended shape is a thin command that moves a distilled subtree into a
long-lived map and stamps provenance:

```
mm.py distil <session-map>:<node-id> --to work:<path> --src <ref>
```

The judgment — *does this belong?* — stays with the human. Only the mechanics and the
provenance stamp are worth automating.

## Requirements

- Python 3, no dependencies
- Freeplane 1.12+ (developed against 1.13.2 using the 1.12.x config directory)
- **Java 8 or 11–23 for Freeplane itself.** The launcher refuses anything newer; if your
  default `java` is too new, set `FREEPLANE_JAVA_HOME` when launching.
