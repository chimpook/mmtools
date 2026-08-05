---
name: mm
description: Mirror the current Claude Code session into a Freeplane mind map and make it two-way. Use when the user types /mm, or asks to switch to mindmap mode, open the session as a mind map, or continue the conversation in Freeplane.
---

# /mm — mirror this session into a Freeplane map

`$MM` below means the mmtools checkout, normally `~/apps/mmtools`. The tool is user-level;
the **map collection** is whatever directory you are working in (it needs `.mm` files and
an `.mmrc` — see `$MM/README.md`). If there is no collection, say so rather than creating
a session map in the wrong place.

Turns the running conversation into a session map and keeps both surfaces live: what
you say in the console also lands in the map, and what the user marks in the map reaches
you here. **This session stays the brain** — nothing is delegated to a sub-process.

Do not use `mmd.py` for this. It spawns separate `claude -p` sessions and exists only for
unattended requests with no terminal session running. See `$MM/docs/BRIDGE.md`.

## Step 1 — build the map from the conversation so far

Freeplane is not open yet, so build in **file mode** with full nesting.

```bash
$MM/mm.py session new "<short task title>"      # prints the path; keep it
```

Then reconstruct the working process to date as a subtree. Not a transcript — a
**digest with structure**, following the collection's own conventions (its `CLAUDE.md`, if it has one):

- the problem as stated, and any constraints the user gave
- what was tried, in parent → child chains: symptom → hypothesis → cause → solution
- findings worth keeping, phrased as assertions
- decisions and *why*, including options that were rejected
- what is still open — `?` for questions, `⧗` for waiting, `⚠` for problems
- long content (code, output, command sequences) goes in **notes**, never node text

Use the collection's icon vocabulary (`$MM/mm.py icons` lists the names). Get IDs back from `$MM/mm.py add` output so you
can nest. Verify with `$MM/mm.py check` before opening it.

## Step 2 — open Freeplane on it

```bash
FREEPLANE_JAVA_HOME=/usr/lib/jvm/java-21-openjdk \
  nohup freeplane "<absolute map path>" >/tmp/fp.log 2>&1 &
```

Two things this gets right, both learned the hard way:

- **`FREEPLANE_JAVA_HOME` is required.** The `java` on this shell's PATH is 26, and the
  launcher refuses anything above 23 — it exits with an ERROR block and no window. The
  user's own desktop launch resolves to 21, which is why it works for them.
- **Use an absolute path.** The launch is backgrounded from a shell whose cwd you should
  not rely on.

Give it ~15s before checking `$MM/mm.py stats | tail -1` — the JVM is slow to boot, and
"not running" a few seconds in means nothing.

Then tell the user, in one short message, to **arm the watcher**: Tools > MM Watch >
Start map watcher. Nothing can reach the map until they do. Confirm with `$MM/mm.py watch`
before relying on it.

## Step 3 — arm the change monitor

`inotifywait` is not installed on this machine, so poll mtime. One event per save:

```
Monitor(
  command: 'M="<map path>"; last=$(stat -c %Y "$M"); while true; do
              cur=$(stat -c %Y "$M" 2>/dev/null || echo "$last");
              if [ "$cur" != "$last" ]; then echo "map saved $(date +%H:%M:%S)"; last=$cur; fi;
              sleep 1; done',
  description: 'session map saves: <basename>',
  persistent: true,
  timeout_ms: 3600000,
)
```

Keep the map path in mind for the rest of the session — every later step needs it.

## Step 4 — the two-way loop

**console → map.** After each substantive reply, post a digest of it into the map. One
command, nesting supported:

```bash
echo '{"text":"<terse headline>","icons":["info"],
       "note":"<detail, if any>",
       "children":[{"text":"<point>"},{"text":"<point>","icons":["problem"]}]}' \
  | $MM/mm.py post "<map path>" <parent-node-id>
```

Rules that matter:

- Post the **shape** of the reply, not its prose. Headline plus a few child points; the
  full text belongs in a note if it is worth keeping at all.
- Skip trivia. Acknowledgements, retries and dead ends you already corrected do not earn
  a node. A map full of noise is worse than a map that lags.
- Parent it sensibly: under the request it answers, or under the root for a new thread.
- If you ask the user a question, post it as a `?` node with the alternatives as
  children so it can be answered in either surface.

**map → console.** When a `map saved` event arrives:

```bash
$MM/mm.py inbox "<map path>"                    # launch-marked nodes not yet handled
```

For each pending node: treat its text (plus any children the user wrote under it) as a
message from the user, answer it **in the console and in the map**, then

```bash
$MM/mm.py icon "<map path>:<node-id>" --set done --live   # visible tick
$MM/mm.py inbox "<map path>" --done <node-id>             # so it does not re-fire
```

Note the `<map path>:` prefix on the icon target. A bare id only resolves inside the
three registered maps (work/archive/store); session maps need the path form.

Both are needed. The tick lands in Freeplane's memory only; the on-disk file keeps its
launch icon until the next save, so `--done` is what actually prevents a repeat.

A `map saved` event with nothing pending means the user just saved — say nothing and
carry on.

## Step 5 — closing the session

When the work is done, offer to **distil anything durable into
a durable branch of the collection's long-lived map**, with a `src:` child and a
verified date. Then the session map can be deleted: it is
gitignored and disposable. That is the whole point of the split — sessions are where
work happens, `state` is what survives it.

## Reattaching

If `/mm` is invoked and `$MM/mm.py session list` already shows a map for this task, do not
start over: reuse it, re-arm the Monitor, and post a `resumed <time>` node so the gap is
visible in the map.

## Failure modes, and what they look like

| Symptom | Cause |
| --- | --- |
| `$MM/mm.py watch` says not armed | Tools > MM Watch > Start map watcher, needed after every Freeplane start |
| nodes never appear | watcher not armed, or the map is not the one open — the watcher scans open maps by canonical path |
| `SKIP map not open` in the Freeplane log | the session map was closed; reopen it |
| a request fires twice | `--done` was not recorded |
| `mm.py` refuses a file write | correct: Freeplane is open. Use `--live` / `post`, never `--force` |

Live mode cannot create nested structure through `add --live`, but `post` can, because
the watcher builds children from the queue command. Deep restructuring still needs
Freeplane closed and file mode.
