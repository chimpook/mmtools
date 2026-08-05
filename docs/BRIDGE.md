# Freeplane as a Claude Code interface (mmd.py)

Read this before changing `mmd.py` or widening its permissions.

Phases 1 and 2 are built and verified. Phase 3 would be cancel, retry and concurrency.

A **session map** is a throwaway Freeplane map, one per task, living in `./sessions/`.
You write requests in it and the replies arrive as child nodes. The Workflow map stays
what it is — a knowledge aggregator — and never carries session chatter.

## The loop

```
mm.py session new "audit the planned branch"      ->  sessions/2026-08-05-audit-....mm
   open it in Freeplane, arm MM Watch
   write a request node, mark it 🚀 launch, SAVE          <- saving is what sends it
        |
        v  mmd.py polls ./sessions/ every second, reads what is ON DISK
   claude -p --session-id <uuid> --output-format stream-json
        |
        v  each event becomes a queue command
   queue.jsonl      ->  MM Watch add-on  ->  nodes appear in the open map
```

Freeplane remains the only process that writes a `.mm` file. The daemon never touches
the file — it reads, and it queues.

Icons: 🚀 `launch` = send · ⧗ `hourglass` = running · ⚙ `executable` = tool call ·
? `help` = options node · ✓ `button_ok` = done / your chosen option · ✗ `button_cancel` = failed.

## Options and choices (phase 2)

The sub-session has no tool for asking a question, so it asks in text and the daemon
turns it into a node. Ending a reply with exactly:

```
OPTIONS: which branch should I audit?
- tips
- info
```

produces a `? which branch should I audit?` node with the options nested under it, and
parks the request on ⧗ rather than ✓ — it is waiting on you, not finished.

**Tick one option with ✓ and save.** The daemon then resumes that session
(`claude -p --resume <session-id>`) and posts further replies **beneath the chosen
option**, so the thread branches where the decision was made. Verified: context carries
across the resume, and a second OPTIONS block can be issued from inside a branch.

Two mechanics worth knowing:

- The daemon never learns the options node's id — the watcher creates it in memory. It
  finds the choice **structurally** instead: under a request in `awaiting_choice`, look
  for a node carrying the `help` icon with a ✓ child. That is why the user must save
  before ticking: the option nodes need real ids on disk.
- Answered choices are recorded per request (`answered: [node ids]`) so re-saving the map
  does not re-fire the same decision.

Nesting works because the queue's `add` op takes a recursive `children` list and the
watcher holds live `Node` objects. `mm.py --live` still cannot nest — that limit is about
*addressing* a node from outside, not about the watcher.

## Two design points that are not obvious

**Saving is sending.** The daemon only sees the saved file. That is deliberate — a
half-written request cannot fire — but it has a consequence: replies live in Freeplane's
memory until you save, so to act on a reply (e.g. choose an option in phase 2) you save
first, which writes the replies to disk, and only then can the daemon see your mark.

**Paths.** The bridge is user-level (`~/.mm-bridge/{queue.jsonl,status.json}`) because
commands carry absolute map paths — one queue serves every collection, and the Groovy
watcher therefore needs no config. Per-project settings live in `.mmrc` at the collection
root (`maps`, `style_from`, `sessions`), found by walking up from the cwd. `MM_HOME`
overrides the search; `MM_BRIDGE_DIR` relocates the bridge for testing.

**Claims are recorded outside the map**, in `sessions/.mmd-state.json`, keyed by
`(map path, node id)`. They have to be: the saved file still carries the `launch` icon
after a run — the ⧗/✓ marks are queued into memory, not written — so without the state
file every request would fire again on the next poll.

## Security — measured, not assumed

Pattern scoping does **not** work in `claude -p`. Verified 2026-07-31:

| Attempt | Result |
| --- | --- |
| `--allowedTools "Bash(./mm.py:*)"` | `id -un` ran anyway |
| `--settings` with `permissions.allow` patterns | `id -un` ran anyway, `permission_denials: []` |
| `--disallowedTools "Bash"` | Bash genuinely absent, and it said so |

So **only whole-tool denial is enforced.** A session map is a file-triggered execution
surface: whatever is written into a launch-marked node runs. Phase 1 is therefore
read-only —

```
allow  Read Grep Glob
deny   Bash Write Edit NotebookEdit WebFetch WebSearch Task Agent
```

The maps are XML, so `Grep`/`Read` answer map questions perfectly well without a shell.

**Granting `Bash` means granting arbitrary shell, not scoped shell.** If that is ever
wanted it is a separate, deliberate decision — not a config tweak. Never use
`--dangerously-skip-permissions` here.

## Phase 1 limits

- **Replies are flat within a turn** — the daemon can only address nodes that exist on
  disk, so a turn's nodes are siblings. Structure comes from the options mechanism, which
  nests because the watcher builds it. Long content goes in notes.
- **The session map must be open in Freeplane** with MM Watch armed, or replies queue up
  and the watcher reports `SKIP map not open` when it finally drains them.
- **One request at a time**, in file order. No cancel yet — a stuck run needs the daemon
  restarting and the state entry removing.

## Running it

```bash
mmd.py            # foreground, Ctrl-C to stop
mmd.py --once     # single pass, for testing
mm.py session list
```

The daemon runs `claude` with `cwd=mm.HERE`, i.e. the collection root it resolved (see
"A map collection" in the README), so every session inherits that collection's
`CLAUDE.md` — the `mm.py` rules, icon vocabulary and audit method come for free.

## Testing without Freeplane

Plant a request directly, run one pass, and read the queue instead of the map:

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
import mm
m = mm.Map('s','sessions/<file>.mm')
m.insert_child(m.root, b'<node TEXT=\"your question\" ID=\"ID_9001\" CREATED=\"1\" MODIFIED=\"1\"><icon BUILTIN=\"launch\"/></node>')
m.save()"
mmd.py --once
python3 -c "
import json
[print(json.loads(l)['op'], json.loads(l).get('text','')[:80]) for l in open('.mm-queue.jsonl')]"
```
