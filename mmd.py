#!/usr/bin/env python3
"""mmd.py - bridge Freeplane session maps to Claude Code.

    mmd.py            run in the foreground (Ctrl-C to stop)
    mmd.py --once     one pass, then exit (handy for testing)

How a turn works
    1. In a session map under ./sessions/, write a request node and mark it with
       the `launch` icon, then SAVE. Saving is what sends it - the daemon only
       ever reads what is on disk.
    2. The daemon claims the request, runs `claude -p` and streams the reply back
       as child nodes through the MM Watch queue, so they appear in the open map
       without the file being written by anything but Freeplane.
    3. The request is marked hourglass while running, then tick or cross.

Why the claim is recorded outside the map: replies live in Freeplane's memory
until you save, so the saved file still carries the launch icon after a run.
A state file keyed by (map, node id) is what stops a request firing twice.

Phase 1 deliberately appends replies flat under the request node - a node created
live is not addressable until the map is saved, so deeper nesting is not possible
yet. Detail goes into notes instead.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))  # symlink-safe
import mm  # noqa: E402  - mm.py is the map library

STATE = os.path.join(mm.SESSIONS, ".mmd-state.json")
POLL = 1.0
MAX_NODE_TEXT = 160
OPTIONS_ICON = "help"          # marks a node whose children are choices

# The sub-session has no tools for asking a question, so it asks in text and the
# daemon turns it into a node. Anchored at line start to avoid false positives.
OPTIONS_RE = re.compile(r"^OPTIONS:[ \t]*(.+?)\s*$((?:\n^[-*][ \t]+.+$)+)", re.M)

# SECURITY, measured rather than assumed.
#
# Pattern scoping does NOT work in `claude -p`. Verified on 2026-07-31:
#   --allowedTools "Bash(./mm.py:*)"           -> `id -un` ran anyway
#   --settings with permissions.allow patterns -> `id -un` ran anyway, no denial
# Only whole-tool denial is enforced:
#   --disallowedTools "Bash"                   -> Bash genuinely absent
#
# A session map is a file-triggered execution surface: anything written into a
# launch-marked node runs. So Bash stays denied and phase 1 is read-only.
# Granting Bash here means granting arbitrary shell, not scoped shell - if that
# is ever wanted it must be a deliberate, separately-reviewed decision.
# Never use --dangerously-skip-permissions.
ALLOWED_TOOLS = ["Read", "Grep", "Glob"]
DENIED_TOOLS = ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch",
                "Task", "Agent"]


def log(msg):
    print("%s  %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def load_state():
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(mm.SESSIONS, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, STATE)


def session_maps():
    if not os.path.isdir(mm.SESSIONS):
        return []
    return [os.path.join(mm.SESSIONS, f)
            for f in sorted(os.listdir(mm.SESSIONS)) if f.endswith(".mm")]


def split_text(s):
    """(node text, note) - keep node titles short, put the body in a note."""
    s = (s or "").strip()
    if not s:
        return "(empty)", ""
    first = s.splitlines()[0].strip()
    if len(s) <= MAX_NODE_TEXT and "\n" not in s:
        return s, ""
    return (first[:MAX_NODE_TEXT] or "(reply)"), s


def emit(map_path, parent_id, text, icons=(), note="", children=None):
    """Append a node (optionally with children) under parent_id, via the queue."""
    t, auto_note = split_text(text)
    cmd = {"op": "add", "map": map_path, "target": parent_id, "text": t,
           "icons": list(icons), "note": note or auto_note}
    if children:
        cmd["children"] = children
    mm.enqueue(cmd)


def parse_options(text):
    """(question, [options], remainder) if the reply ends in an OPTIONS block."""
    m = OPTIONS_RE.search(text or "")
    if not m:
        return None, [], text
    question = m.group(1).strip()
    opts = [re.sub(r"^[-*][ \t]+", "", l).strip()
            for l in m.group(2).strip().splitlines() if l.strip()]
    remainder = (text[:m.start()]).strip()
    return question, [o for o in opts if o], remainder


def post_options(map_path, parent_id, question, options):
    """One command: the question node with its options nested underneath."""
    emit(map_path, parent_id, "? " + question, icons=[OPTIONS_ICON],
         children=[{"text": o, "icons": []} for o in options],
         note="Mark one option with the tick icon and save - that answers it.")


def set_icons(map_path, node_id, icons, clear=True):
    mm.enqueue({"op": "icon", "map": map_path, "target": node_id,
                "clear": clear, "icons": list(icons)})


def build_prompt(node):
    """The request is the node text plus any subtree the user wrote under it."""
    lines = []

    def walk(n, d):
        if n.text:
            lines.append("%s%s" % ("  " * d, n.text))
        for k in n.kids:
            walk(k, d + 1)

    walk(node, 0)
    body = "\n".join(lines)
    return (
        "You are answering inside a Freeplane session map, driven by mmd.py.\n"
        "Keep replies compact: short statements suit map nodes. Long output is "
        "fine - it goes into a note.\n"
        "You have read-only tools only: Read, Grep, Glob. There is no shell and "
        "no write access. Maps are Freeplane XML in the collection root - grep them "
        "directly. "
        "If a request needs a tool you lack, say so plainly rather than guessing.\n"
        "If you need the user to decide something, end your reply with exactly:\n"
        "OPTIONS: <the question>\n- <first option>\n- <second option>\n"
        "and nothing after it. Only do that when a real choice is needed.\n\n"
        "Request:\n%s" % body)


def run_claude(prompt, session_id, resume=False):
    cmd = ["claude", "-p", prompt,
           "--resume" if resume else "--session-id", session_id,
           "--output-format", "stream-json", "--verbose",
           "--allowedTools"] + ALLOWED_TOOLS + ["--disallowedTools"] + DENIED_TOOLS
    return subprocess.Popen(cmd, cwd=mm.HERE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)


def handle_event(ev, map_path, req_id, seen):
    """Translate one stream-json event into map nodes. Returns final text or None.

    `seen` collects assistant text already emitted: the closing `result` event
    repeats the last assistant message, and emitting both duplicates the answer.
    """
    kind = ev.get("type")
    if kind == "assistant":
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                # Strip any OPTIONS block: the result handler turns it into a
                # proper options node, and posting the raw text too duplicates it.
                _, _, prose = parse_options(block["text"])
                if prose.strip():
                    emit(map_path, req_id, prose)
                    seen.append(prose.strip())
            elif block.get("type") == "tool_use":
                name = block.get("name", "tool")
                inp = block.get("input", {}) or {}
                brief = (inp.get("command") or inp.get("pattern")
                         or inp.get("file_path") or inp.get("description") or "")
                emit(map_path, req_id, "%s: %s" % (name, str(brief)[:120]),
                     icons=[mm.TOOLCALL],
                     note=json.dumps(inp, indent=1)[:1500] if inp else "")
    elif kind == "result":
        if ev.get("subtype") == "success":
            return ev.get("result") or ""
        emit(map_path, req_id, "run failed: %s" % ev.get("subtype"),
             icons=[mm.FAILED], note=json.dumps(ev)[:1500])
        return None
    return None


def find_pending_choice(request_node, answered):
    """(options_node, chosen_child) once the user ticks an option, else (None, None)."""
    stack = list(request_node.kids)
    while stack:
        n = stack.pop()
        if OPTIONS_ICON in n.icons:
            for k in n.kids:
                if mm.DONE in k.icons and k.id not in answered:
                    return n, k
        stack.extend(n.kids)
    return None, None


def run_turn(map_path, reply_to, key, prompt, sid, st, resume=False):
    """Stream one Claude turn into the map. Handles an OPTIONS reply by posting the
    choices and parking the request until the user ticks one."""
    request_id = key.split("::")[1]
    set_icons(map_path, request_id, [mm.RUNNING])
    armed, _ = mm.watcher_state()
    if not armed:
        log("     warning: MM Watch is not armed - replies will queue up")

    proc = run_claude(prompt, sid, resume=resume)
    final, seen = None, []
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            got = handle_event(ev, map_path, reply_to, seen)
            if got is not None:
                final = got
        proc.wait(timeout=30)
    except Exception as e:                       # noqa: BLE001 - report, never crash
        log("     error: %s" % e)
        emit(map_path, reply_to, "bridge error: %s" % e, icons=[mm.FAILED])

    err = (proc.stderr.read() or "").strip() if proc.stderr else ""
    if final is None:
        set_icons(map_path, request_id, [mm.FAILED])
        st[key]["status"] = "failed"
        if err:
            emit(map_path, reply_to, "claude stderr", icons=[mm.FAILED], note=err[:2000])
        log("     failed rc=%s %s" % (proc.returncode, err[:200]))
        save_state(st)
        return

    question, options, remainder = parse_options(final)
    if question and options:
        if remainder and remainder not in seen:
            emit(map_path, reply_to, remainder)
        post_options(map_path, reply_to, question, options)
        st[key]["status"] = "awaiting_choice"
        st[key]["session"] = sid
        # the request stays hourglass: it is waiting on you, not finished
        log("     awaiting choice: %s (%d options)" % (question[:60], len(options)))
    else:
        if final.strip() not in seen:
            emit(map_path, reply_to, final)
        set_icons(map_path, request_id, [mm.DONE])
        st[key]["status"] = "done"
        log("     done (%d chars)" % len(final))
    save_state(st)


def process(map_path, node, st):
    key = "%s::%s" % (os.path.abspath(map_path), node.id)
    sid = str(uuid.uuid4())
    st[key] = {"status": "running", "session": sid, "started": time.time(),
               "text": node.label(70), "answered": []}
    save_state(st)
    log("run  %s  %s" % (os.path.basename(map_path), node.label(60)))
    run_turn(map_path, node.id, key, build_prompt(node), sid, st)


def resume_choice(map_path, request, options_node, chosen, st, key):
    """Continue the session from the ticked option, replying beneath it."""
    sid = st[key].get("session")
    if not sid:
        log("     cannot resume: no session id recorded")
        return
    st[key].setdefault("answered", []).append(chosen.id)
    st[key]["status"] = "running"
    save_state(st)
    log("pick %s  %s" % (os.path.basename(map_path), chosen.label(60)))
    prompt = ("The user chose this option: %s\n\n"
              "(The question was: %s)\n"
              "Continue from that choice. Same rules as before: read-only tools, "
              "compact replies, and use the OPTIONS block again only if another "
              "real decision is needed." % (chosen.text, options_node.text))
    # replies nest under the chosen option, which is where the thread continues
    run_turn(map_path, chosen.id, key, prompt, sid, st, resume=True)


def scan_once(st):
    handled = 0
    for path in session_maps():
        try:
            m = mm.Map("session", path)
        except Exception as e:                   # a half-saved file; try again later
            log("skip %s (%s)" % (os.path.basename(path), e))
            continue
        abspath = os.path.abspath(path)

        # 1. requests parked on a choice the user has now made
        for key, rec in list(st.items()):
            if not key.startswith(abspath + "::") or rec.get("status") != "awaiting_choice":
                continue
            req = m.by_id.get(key.split("::")[1])
            if req is None:
                continue
            opts, chosen = find_pending_choice(req, rec.get("answered", []))
            if chosen is not None:
                handled += 1
                resume_choice(path, req, opts, chosen, st, key)

        # 2. brand-new requests
        for n in m.nodes:
            if mm.TRIGGER not in n.icons:
                continue
            key = "%s::%s" % (abspath, n.id)
            if key in st:
                continue
            handled += 1
            process(path, n, st)
    return handled


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    args = ap.parse_args()

    os.makedirs(mm.SESSIONS, exist_ok=True)
    st = load_state()
    log("watching %s   trigger: %s" % (mm.SESSIONS, mm.TRIGGER))
    log("read-only: allow %s | deny %s"
        % (" ".join(ALLOWED_TOOLS), " ".join(DENIED_TOOLS)))
    if args.once:
        n = scan_once(st)
        log("one pass: %d request(s)" % n)
        return
    try:
        while True:
            scan_once(st)
            time.sleep(POLL)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
