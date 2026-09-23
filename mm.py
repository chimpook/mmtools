#!/usr/bin/env python3
"""mm.py - read and edit Freeplane maps without losing a byte.

Every write is a byte-level splice into the original file: the untouched parts
of the map are never re-serialized, so styles, formatting, attribute order and
rich text survive exactly as Freeplane wrote them.

Addressing a node
    store:tips/docker      map alias + slash path (segments match child text)
    work:ID_1842495847     map alias + node id
    ID_1842495847          bare id, searched across all maps
    work:                  the map's root node
Node text in this map contains both '/' and '::', so paths are a convenience for
shallow, clean names.  For anything deeper, get an id from `find` and use that.

Commands
    tree [target] [-d N]              structure with subtree sizes
    find <regex> [-i ICON] [-n N]     search text across maps, prints ids
    show <target>                     one node in full
    add <parent> <text> [...]         append a child
    icon <target> --set X | --clear   set or clear status icon
    note <target> <text>              attach/replace a note
    rename <target> <text>            change node text
    rm <target> --yes                 delete a node and its subtree
    mv <target> --to <parent>         move a subtree, across maps too
    icons                             list the status vocabulary
    stats | check
"""
import argparse
import fcntl
import html
import json
import os
import re
import subprocess
import sys
import time
import xml.parsers.expat as expat

# realpath, not abspath: the entry point is normally a symlink (~/.local/bin/mm.py
# or a collection's ./mm.py), and this must land in the repo the code lives in.
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))


def _find_root():
    """Nearest ancestor of cwd holding .mmrc, else $MM_HOME, else the script's dir.

    Lets one copy of mm.py serve several map collections: the project decides
    which maps exist and what they are called, not the code.

    cwd is searched before MM_HOME so that a collection you have cd'd into always
    wins over the default one — MM_HOME only answers "which collection did you
    mean?" when the cwd is not inside one at all, which is what makes the aliases
    usable from an arbitrary directory.
    """
    d = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, ".mmrc")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    home = os.environ.get("MM_HOME")
    return os.path.abspath(home) if home else SCRIPT_DIR


HERE = _find_root()          # the map collection's root


def _load_config():
    try:
        with open(os.path.join(HERE, ".mmrc"), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


CONFIG = _load_config()

# alias -> filename.  Without .mmrc there are no aliases and maps are addressed
# by path (SomeMap.mm:ID_123), which always works.
MAPS = CONFIG.get("maps") or {}

# Which alias a target with no "alias:" prefix refers to.  Configurable because
# hard-coding it ("work") broke the moment a collection renamed its maps.
DEFAULT_MAP = CONFIG.get("default") or (sorted(MAPS)[0] if MAPS else None)

# friendly name -> Freeplane BUILTIN icon, taken from how this map already uses them
ICONS = {
    "done":     "button_ok",
    "problem":  "messagebox_warning",
    "failed":   "button_cancel",
    "dropped":  "stop-sign",
    "info":     "info",
    "idea":     "idea",
    "question": "help",
    "waiting":  "hourglass",
    "next":     "forward",
    "active":   "wizard",
    "good":     "very_positive",
    "meh":      "smiley-neutral",
    "stop":     "stop",
    "folder":   "folder",
    "bookmark": "bookmark",
    "person":   "male2",
    "group":    "group",
    "p1":       "full-1",
    "tick":     "yes",
}
BUILTIN2NAME = {v: k for k, v in ICONS.items()}
MARK = {
    "button_ok": "✓", "messagebox_warning": "⚠", "button_cancel": "✗",
    "stop-sign": "⊘", "info": "ⓘ", "idea": "✦", "help": "?",
    "hourglass": "⧗", "forward": "➜", "wizard": "⚙", "very_positive": "✓✓",
    "smiley-neutral": "⊙", "stop": "■", "folder": "▸", "bookmark": "⚑",
    "male2": "☻", "group": "☷", "full-1": "①", "yes": "✓",
}


def die(msg):
    print("error: %s" % msg, file=sys.stderr)
    sys.exit(1)


def esc(s):
    """Encode a Python string for an XML attribute the way Freeplane does."""
    s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
          .replace('"', "&quot;"))
    return s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "&#xa;")


def esc_text(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def freeplane_running():
    """True if a Freeplane JVM is up.  Matches java processes only, so our own
    shell (whose command line may mention freeplane) is not a false positive."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return False
    me = {str(os.getpid()), str(os.getppid())}
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid in me:
            continue
        if "freeplane" in args.lower() and re.search(r"(^|/)java\b|jre|jdk", args):
            return True
    return False


# --- live mode: hand edits to the Freeplane watcher instead of the file ------
# The bridge lives at a fixed USER-level path, not per project. Queue commands
# carry the absolute map path, so one queue serves every project and the
# Freeplane watcher needs no configuration at all - which is why this is a
# constant and not a config key.
BRIDGE = os.environ.get("MM_BRIDGE_DIR") or os.path.join(
    os.path.expanduser("~"), ".mm-bridge")
QUEUE = os.path.join(BRIDGE, "queue.jsonl")
STATUS = os.path.join(BRIDGE, "status.json")
BEAT_STALE = 12.0  # seconds; watcher heartbeats every ~3s


def watcher_state():
    """(armed, info) - whether the in-Freeplane watcher is alive right now."""
    try:
        with open(STATUS, encoding="utf-8") as fh:
            info = json.load(fh)
    except Exception:
        return False, None
    fresh = (time.time() - info.get("ts", 0) / 1000.0) < BEAT_STALE
    return bool(info.get("armed")) and fresh, info


SESSIONS = os.path.join(HERE, CONFIG.get("sessions") or "sessions")

# icons the Freeplane<->Claude bridge uses (all verified Freeplane builtins)
TRIGGER = "launch"        # you mark this and save -> the request is sent
RUNNING = "hourglass"
TOOLCALL = "executable"
FAILED = "button_cancel"
DONE = "button_ok"


def style_hooks(required=True):
    """The MapStyle + edge-colour hooks, lifted from the working map so a new
    session map renders icons and colours identically.  With required=False a
    collection without maps yields b"" and Freeplane applies its defaults."""
    src = CONFIG.get("style_from") or (sorted(MAPS)[0] if MAPS else None)
    if src in MAPS:
        src = MAPS[src]
    if not src:
        cands = sorted(f for f in os.listdir(HERE) if f.endswith(".mm"))
        if not cands:
            if not required:
                return b""
            die("no .mm file in %s to take a MapStyle from" % HERE)
        src = cands[0]
    data = open(os.path.join(HERE, src), "rb").read()
    m = re.search(rb'<hook NAME="MapStyle".*?</hook>', data, re.S)
    if not m:
        if not required:
            return b""
        die("cannot find MapStyle hook in %s" % src)
    out = m.group()
    e = re.search(rb'<hook NAME="AutomaticEdgeColor"[^>]*/>', data)
    if e:
        out += b"\n" + e.group()
    return out


def new_session(title):
    """Create a styled, empty session map and return its path."""
    os.makedirs(SESSIONS, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "session"
    stamp = time.strftime("%Y-%m-%d")
    path = os.path.join(SESSIONS, "%s-%s.mm" % (stamp, slug))
    n = 2
    while os.path.exists(path):
        path = os.path.join(SESSIONS, "%s-%s-%d.mm" % (stamp, slug, n))
        n += 1
    now = int(time.time() * 1000)
    note = ("<richcontent TYPE=\"NOTE\" CONTENT-TYPE=\"xml/\">\n<html>\n  <head>\n  </head>\n"
            "  <body>\n"
            "    <p>Write a request as a child node, mark it with the launch icon, then SAVE.</p>\n"
            "    <p>Saving is what sends it. Replies arrive as child nodes of the request.</p>\n"
            "    <p>The request is marked with an hourglass while running, a tick when done.</p>\n"
            "    <p>MM Watch must be armed: Tools &gt; MM Watch &gt; Start map watcher.</p>\n"
            "  </body>\n</html>\n</richcontent>")
    xml = (b'<map version="freeplane 1.12.15">\n'
           b'<!--To view this file, download free mind mapping software Freeplane from '
           b'https://www.freeplane.org -->\n'
           b'<node TEXT="' + esc(title).encode("utf-8") + b'" FOLDED="false" ID="ID_1" '
           b'CREATED="%d" MODIFIED="%d" STYLE="oval">\n' % (now, now)
           + b'<font SIZE="18"/>\n' + style_hooks() + b"\n"
           + note.encode("utf-8") + b"\n</node>\n</map>\n")
    with open(path, "wb") as fh:
        fh.write(xml)
    expat.ParserCreate().Parse(open(path, "rb").read(), True)   # never emit broken XML
    return path


def wait_for_result(before, timeout=8.0):
    """Block until the watcher reports one more outcome, then return its text.

    Live commands resolve targets against the on-disk file, so a node created
    live is unaddressable until the map is saved.  The watcher puts the created
    id in its status ("OK add ID_x under ID_y"), so reading it back makes live
    edits compositional without a save in between.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        armed, info = watcher_state()
        if info:
            done = info.get("applied", 0) + info.get("failed", 0)
            if done > before:
                return info.get("last", "")
        time.sleep(0.15)
    return None


def outcomes_so_far():
    _, info = watcher_state()
    if not info:
        return 0
    return info.get("applied", 0) + info.get("failed", 0)


def enqueue(obj):
    """Append one command, locked, so the watcher cannot drain a partial line."""
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    os.makedirs(os.path.dirname(QUEUE), exist_ok=True)
    with open(QUEUE, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    armed, _ = watcher_state()
    if not armed:
        print("warning: watcher is not armed - command sits in the queue until\n"
              "         Tools > MM Watch > Start map watcher",
              file=sys.stderr)
    return line


class Node:
    __slots__ = ("id", "text", "start", "inner", "end", "selfclose", "parent",
                 "kids", "icons", "link", "depth", "map")

    def __init__(self):
        self.kids, self.icons, self.link, self.text = [], [], None, None

    @property
    def marks(self):
        return "".join(MARK.get(i, "●") for i in self.icons)

    def label(self, width=96):
        t = (self.text if self.text is not None else "[rich text]")
        t = " ".join(t.split())
        if len(t) > width:
            t = t[:width - 1] + "…"
        return t

    def size(self):
        return 1 + sum(k.size() for k in self.kids)


class Map:
    def __init__(self, alias, path):
        self.alias, self.path = alias, path
        self.data = open(path, "rb").read()
        self.dirty = False
        self._index()

    # ---------- parsing ----------

    @staticmethod
    def _tag_end(data, start):
        """Offset just past the start tag's '>', plus whether it self-closes.
        Quote-aware, because attribute values contain '>' (e.g. '=>' in code)."""
        i, inq = start, False
        while i < len(data):
            c = data[i:i + 1]
            if c == b'"':
                inq = not inq
            elif not inq and c == b">":
                return i + 1, data[i - 1:i] == b"/"
            i += 1
        raise ValueError("unterminated tag at %d" % start)

    def _index(self):
        self.nodes, self.by_id = [], {}
        data = self.data
        stack, ndepth = [], []
        p = expat.ParserCreate()

        def start(name, attrs):
            if name == "node":
                n = Node()
                n.map = self
                n.id = attrs.get("ID")
                n.text = attrs.get("TEXT")
                n.link = attrs.get("LINK")
                n.start = p.CurrentByteIndex
                n.inner, n.selfclose = self._tag_end(data, n.start)
                n.depth = len(ndepth)
                n.parent = ndepth[-1] if ndepth else None
                if n.parent is not None:
                    n.parent.kids.append(n)
                self.nodes.append(n)
                if n.id:
                    self.by_id[n.id] = n
                ndepth.append(n)
            elif name == "icon" and ndepth:
                # only icons belonging to the node itself, not to a nested stylenode
                if stack and stack[-1] == "node":
                    ndepth[-1].icons.append(attrs.get("BUILTIN"))
            stack.append(name)

        def end(name):
            stack.pop()
            if name != "node":
                return
            n = ndepth.pop()
            idx = p.CurrentByteIndex
            close = b"</node>"
            n.end = idx + len(close) if data[idx:idx + len(close)] == close else idx

        p.StartElementHandler, p.EndElementHandler = start, end
        p.Parse(data, True)
        self.root = self.nodes[0] if self.nodes else None

    # ---------- lookup ----------

    def resolve(self, spec, start=None):
        spec = (spec or "").strip()
        if not spec:
            return start or self.root
        if re.fullmatch(r"ID_\d+", spec):
            n = self.by_id.get(spec)
            if not n:
                die("id %s not found in %s" % (spec, self.alias))
            return n
        cur = start or self.root
        for seg in [s for s in spec.split("/") if s.strip()]:
            key = seg.strip().lower()
            exact = [k for k in cur.kids if (k.text or "").strip().lower() == key]
            cands = exact or [k for k in cur.kids
                              if (k.text or "").strip().lower().startswith(key)]
            if not cands:
                sample = ", ".join(repr(k.label(30)) for k in cur.kids[:8])
                die("no child %r under %r (children: %s)" % (seg, cur.label(40), sample))
            if len(cands) > 1:
                print("ambiguous %r under %r:" % (seg, cur.label(40)), file=sys.stderr)
                for c in cands[:10]:
                    print("   %s  %s" % (c.id, c.label(60)), file=sys.stderr)
                die("use an id instead")
            cur = cands[0]
        return cur

    def path_of(self, n):
        parts, cur = [], n
        while cur is not None:
            parts.append(cur.label(34))
            cur = cur.parent
        return "%s:%s" % (self.alias, " / ".join(reversed(parts[:-1])) or "<root>")

    # ---------- editing ----------

    def _splice(self, lo, hi, blob):
        self.data = self.data[:lo] + blob + self.data[hi:]
        self.dirty = True

    def _touch(self, n):
        """Bump MODIFIED inside a node's start tag."""
        tag = self.data[n.start:n.inner]
        new = re.sub(rb'MODIFIED="\d+"', b'MODIFIED="%d"' % int(time.time() * 1000), tag)
        if new != tag:
            self._splice(n.start, n.inner, new)

    def next_id(self, seen):
        base = max([int(m) for m in re.findall(rb'ID="ID_(\d+)"', self.data)] or [1])
        i = base + 1
        while "ID_%d" % i in seen:
            i += 1
        seen.add("ID_%d" % i)
        return "ID_%d" % i

    def insert_child(self, parent, blob, first=False):
        if parent.selfclose:
            # '<node .../>' has to become '<node ...>child</node>'
            tag = self.data[parent.start:parent.inner]
            assert tag.endswith(b"/>")
            self._splice(parent.start, parent.inner,
                         tag[:-2] + b">\n" + blob + b"\n</node>")
        elif first:
            self._splice(parent.inner, parent.inner, b"\n" + blob)
        else:
            self._splice(parent.end - len(b"</node>"), parent.end - len(b"</node>"),
                         blob + b"\n")
        self.reindex()

    def cut(self, n):
        """Remove a node, returning its exact bytes."""
        if n is self.root:
            die("refusing to delete the root node")
        blob = self.data[n.start:n.end]
        lo, hi = n.start, n.end
        while self.data[hi:hi + 1] in (b"\n", b"\r"):
            hi += 1
        self._splice(lo, hi, b"")
        self.reindex()
        return blob

    def set_color(self, n, color):
        """Set (or replace) the COLOR attribute in a node's start tag."""
        tag = self.data[n.start:n.inner]
        blob = color.encode()
        if re.search(rb'COLOR="[^"]*"', tag):
            new = re.sub(rb'COLOR="[^"]*"', b'COLOR="%s"' % blob, tag)
        elif tag.endswith(b"/>"):
            new = tag[:-2] + b' COLOR="%s"/>' % blob
        else:
            new = tag[:-1] + b' COLOR="%s">' % blob
        if new != tag:
            self._splice(n.start, n.inner, new)
            self.reindex()

    def set_icons(self, n, builtins):
        body = self.data[n.inner:(n.end - len(b"</node>") if not n.selfclose else n.inner)]
        keep = re.sub(rb'<icon BUILTIN="[^"]*"/>\s*', b"", body, count=len(n.icons)) \
            if n.icons else body
        new = b"".join(b'<icon BUILTIN="%s"/>' % b.encode() for b in builtins)
        if n.selfclose:
            if not builtins:
                return
            tag = self.data[n.start:n.inner]
            self._splice(n.start, n.inner, tag[:-2] + b">" + new + b"</node>")
        else:
            self._splice(n.inner, n.end - len(b"</node>"), new + keep)
        self.reindex()

    def set_note(self, n, text):
        note = ('<richcontent TYPE="NOTE" CONTENT-TYPE="xml/">\n<html>\n  <head>\n  '
                '</head>\n  <body>\n' +
                "".join("    <p>\n      %s\n    </p>\n" % esc_text(ln or "")
                        for ln in text.split("\n")) +
                '  </body>\n</html>\n</richcontent>')
        blob = note.encode("utf-8")
        if n.selfclose:
            tag = self.data[n.start:n.inner]
            self._splice(n.start, n.inner, tag[:-2] + b">\n" + blob + b"\n</node>")
        else:
            body_lo, body_hi = n.inner, n.end - len(b"</node>")
            body = self.data[body_lo:body_hi]
            body = re.sub(rb'<richcontent TYPE="NOTE".*?</richcontent>\s*', b"", body,
                          flags=re.S)
            # a note goes after icons, before child nodes
            m = re.match(rb'((?:\s*<icon BUILTIN="[^"]*"/>)*)', body)
            at = m.end() if m else 0
            self._splice(body_lo, body_hi, body[:at] + b"\n" + blob + b"\n" + body[at:])
        self.reindex()

    def rename(self, n, text):
        tag = self.data[n.start:n.inner]
        if b'TEXT="' not in tag:
            die("node %s stores its text as rich content; edit it in Freeplane" % n.id)
        new = re.sub(rb'TEXT="[^"]*"', b'TEXT="' + esc(text).encode("utf-8") + b'"', tag,
                     count=1)
        self._splice(n.start, n.inner, new)
        self.reindex()

    def reindex(self):
        self._index()

    def save(self):
        if not self.dirty:
            return False
        try:  # never write a file we cannot parse
            expat.ParserCreate().Parse(self.data, True)
        except expat.ExpatError as e:
            die("refusing to save %s - result would be malformed XML (%s)" % (self.path, e))
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(self.data)
        os.replace(tmp, self.path)
        self.dirty = False
        return True


# ---------------- helpers ----------------

def load(alias):
    if alias not in MAPS:
        die("no map alias %r (configure .mmrc, or address by path: file.mm:ID_1)" % alias)
    path = os.path.join(HERE, MAPS[alias])
    if not os.path.exists(path):
        die("missing map file %s" % path)
    return Map(alias, path)


def all_maps():
    return [load(a) for a in MAPS]


def parse_target(spec, default=None):
    """'store:tips/docker' -> (Map, Node).  Bare ids are searched everywhere."""
    spec = (spec or "").strip()
    if not spec:
        # An empty target used to silently mean "the map root", which turned a
        # failed id lookup in a shell pipeline into nine nodes glued onto the
        # root of the working map.  Require the explicit "work:" form instead.
        die("empty target - write 'work:' (or 'store:'/'archive:') to mean the map root")
    alias, sep, rest = spec.partition(":")
    if sep and alias in MAPS:
        m = load(alias)
        return m, m.resolve(rest)
    # A path to any .mm file, so session maps are addressable too:
    #   sessions/2026-08-04-foo.mm:ID_123
    # A bare id only ever searched the three registered maps, which made the
    # /mm skill's own tick command fail on its first real round trip.
    if sep and alias.endswith(".mm"):
        path = alias if os.path.isabs(alias) else os.path.join(HERE, alias)
        if not os.path.exists(path):
            die("no such map file: %s" % path)
        m = Map(os.path.basename(alias), path)
        return m, m.resolve(rest)
    if re.fullmatch(r"ID_\d+", spec):
        for m in all_maps():
            if spec in m.by_id:
                return m, m.by_id[spec]
        die("id %s not found in any map" % spec)
    alias_ = default or DEFAULT_MAP
    if not alias_:
        die("no default map: prefix the target with an alias, or set \"default\" in .mmrc")
    m = load(alias_)
    return m, m.resolve(spec)


def icon_builtin(name):
    if name in ICONS:
        return ICONS[name]
    if name in BUILTIN2NAME or re.fullmatch(r"[a-z0-9_-]+", name or ""):
        return name  # allow raw Freeplane builtins
    die("unknown icon %r (try: mm.py icons)" % name)


def guard(args):
    if getattr(args, "force", False):
        return
    if freeplane_running():
        die("Freeplane is running - it would overwrite these edits on its next save.\n"
            "       Close it first, or pass --force if you know the map is not open.")


# ---------------- commands ----------------

def cmd_tree(args):
    m, n = parse_target(args.target or "work:")
    print("%s   [%d nodes]\n" % (m.path_of(n), n.size()))

    def walk(x, d):
        pad = "  " * d
        size = x.size()
        tail = "  [%d]" % size if size > 1 else ""
        mk = (x.marks + " ") if x.icons else ""
        print("%s%s%s%s" % (pad, mk, x.label(88 - len(pad)), tail))
        if d < args.depth:
            for k in x.kids:
                walk(k, d + 1)
        elif x.kids:
            print("%s  … %d more level(s)" % (pad, 1))

    walk(n, 0)


def cmd_find(args):
    try:
        rx = re.compile(args.pattern, re.I)
    except re.error as e:
        die("bad regex: %s" % e)
    want = icon_builtin(args.icon) if args.icon else None
    maps = [load(args.map)] if args.map else all_maps()
    hits = 0
    for m in maps:
        for n in m.nodes:
            if want and want not in n.icons:
                continue
            hay = n.text if n.text is not None else ""
            if not rx.search(hay):
                continue
            hits += 1
            if hits > args.limit:
                print("\n… more matches, raise -n to see them")
                return
            print("%s  %-14s %s%s" % (n.id, m.alias, (n.marks + " ") if n.icons else "",
                                      n.label(84)))
            print("    %s" % m.path_of(n.parent) if n.parent else "")
    if not hits:
        print("no matches")


def cmd_show(args):
    m, n = parse_target(args.target)
    print("id     : %s" % n.id)
    print("map    : %s" % m.path)
    print("path   : %s" % m.path_of(n))
    print("icons  : %s" % (", ".join("%s (%s)" % (BUILTIN2NAME.get(i, "?"), i)
                                     for i in n.icons) or "-"))
    if n.link:
        print("link   : %s" % n.link)
    raw = m.data[n.start:n.inner].decode("utf-8", "replace")
    for k in ("CREATED", "MODIFIED"):
        mt = re.search(k + r'="(\d+)"', raw)
        if mt:
            print("%-7s: %s" % (k.lower(),
                                time.strftime("%Y-%m-%d %H:%M",
                                              time.localtime(int(mt.group(1)) / 1000))))
    print("kids   : %d direct, %d in subtree" % (len(n.kids), n.size() - 1))
    print("\ntext:\n%s" % html.unescape(n.text) if n.text is not None else "\n[rich text]")
    body = m.data[n.inner:n.end].decode("utf-8", "replace")
    mt = re.search(r'<richcontent TYPE="NOTE".*?</richcontent>', body, re.S)
    if mt:
        txt = re.sub(r"<[^>]+>", "", mt.group())
        print("\nnote:\n%s" % html.unescape("\n".join(
            l.strip() for l in txt.splitlines() if l.strip())))
    if n.kids:
        print("\nchildren:")
        for k in n.kids:
            print("  %s  %s%s" % (k.id, (k.marks + " ") if k.icons else "", k.label(76)))


def cmd_add(args):
    m, parent = parse_target(args.parent)
    if args.live:
        before = outcomes_so_far()
        enqueue({"op": "add", "map": m.path, "target": parent.id, "text": args.text,
                 "icons": [icon_builtin(i) for i in (args.icon or [])],
                 "note": args.note or "", "color": args.color or ""})
        res = wait_for_result(before)
        if res is None:
            print("queued: add under %s  (watcher did not report back)" % parent.id)
        else:
            mt = re.search(r"OK add (ID_\d+)", res)
            # print the new id so it can be used as a parent for the next --live add
            print("%s%s" % (res, ("   new=" + mt.group(1)) if mt else ""))
        return
    guard(args)
    seen = set(m.by_id)
    nid = m.next_id(seen)
    now = int(time.time() * 1000)
    bits = ['<node TEXT="%s" ID="%s" CREATED="%d" MODIFIED="%d"'
            % (esc(args.text), nid, now, now)]
    if args.color:
        bits.append(' COLOR="%s"' % esc(args.color))
    if args.link:
        bits.append(' LINK="%s"' % esc(args.link))
    open_tag = "".join(bits) + ">"
    inner = ""
    for name in args.icon or []:
        inner += '<icon BUILTIN="%s"/>' % icon_builtin(name)
    blob = (open_tag + inner + "</node>").encode("utf-8")
    m.insert_child(parent, blob, first=args.first)
    if args.note:
        m.set_note(m.by_id[nid], args.note)
    m._touch(parent)
    m.reindex()
    m.save()
    print("added %s under %s" % (nid, m.path_of(m.by_id[nid].parent)))
    print("  %s" % m.by_id[nid].label(88))


def cmd_color(args):
    if not re.match(r"^#[0-9a-fA-F]{6}$", args.color):
        die("color must be a #rrggbb hex value, got: %s" % args.color)
    m, n = parse_target(args.target)
    if args.live:
        enqueue({"op": "color", "map": m.path, "target": n.id, "color": args.color})
        print("queued: color on %s  %s" % (n.id, n.label(60)))
        return
    guard(args)
    m.set_color(n, args.color)
    n = m.by_id[n.id]
    m._touch(n)
    m.reindex()
    m.save()
    print("colored %s %s  %s" % (n.id, args.color, n.label(60)))


def cmd_icon(args):
    m, n = parse_target(args.target)
    if args.live:
        if args.clear:
            payload = {"clear": True, "icons": []}
        elif args.add:
            payload = {"clear": False, "icons": [icon_builtin(x) for x in args.add]}
        else:
            payload = {"clear": True, "icons": [icon_builtin(x) for x in args.set]}
        enqueue(dict({"op": "icon", "map": m.path, "target": n.id}, **payload))
        print("queued: icon on %s  %s" % (n.id, n.label(60)))
        return
    guard(args)
    if args.clear:
        new = []
    elif args.add:
        new = list(n.icons) + [icon_builtin(x) for x in args.add]
    else:
        new = [icon_builtin(x) for x in args.set]
    m.set_icons(n, new)
    n = m.by_id[n.id]
    m._touch(n)
    m.reindex()
    m.save()
    print("%s  %s%s" % (n.id, (m.by_id[n.id].marks + " ") if new else "", n.label(80)))


def cmd_note(args):
    m, n = parse_target(args.target)
    if args.live:
        enqueue({"op": "note", "map": m.path, "target": n.id, "note": args.text})
        print("queued: note on %s  %s" % (n.id, n.label(60)))
        return
    guard(args)
    m.set_note(n, args.text)
    m._touch(m.by_id[n.id])
    m.reindex()
    m.save()
    print("note set on %s  %s" % (n.id, n.label(70)))


def cmd_rename(args):
    m, n = parse_target(args.target)
    if args.live:
        enqueue({"op": "text", "map": m.path, "target": n.id, "text": args.text})
        print("queued: rename %s  %s" % (n.id, n.label(60)))
        return
    guard(args)
    old = n.label(60)
    m.rename(n, args.text)
    m._touch(m.by_id[n.id])
    m.reindex()
    m.save()
    print("%s\n  was: %s\n  now: %s" % (n.id, old, m.by_id[n.id].label(80)))


def cmd_rm(args):
    guard(args)
    m, n = parse_target(args.target)
    size, lab = n.size(), n.label(70)
    if not args.yes:
        die("would delete %s (%d nodes): %s\n       re-run with --yes" % (n.id, size, lab))
    m.cut(n)
    m.save()
    print("deleted %s (%d nodes): %s" % (n.id, size, lab))


def cmd_mv(args):
    guard(args)
    src_map, src = parse_target(args.target)
    dst_map, dst = parse_target(args.to)
    if src_map.alias == dst_map.alias:
        if dst.start >= src.start and dst.end <= src.end:
            die("cannot move a node into its own subtree")
        blob = src_map.cut(src)
        dst = src_map.resolve(dst.id)
        src_map.insert_child(dst, blob, first=args.first)
        src_map.save()
        print("moved %d nodes to %s" % (_count(blob), src_map.path_of(dst)))
    else:
        blob = src_map.data[src.start:src.end]
        clash = set(re.findall(rb'ID="(ID_\d+)"', blob)) & set(
            k.encode() for k in dst_map.by_id)
        if clash:
            die("%d node id(s) already exist in %s; not moving" % (len(clash),
                                                                   dst_map.alias))
        dst_map.insert_child(dst, blob, first=args.first)
        src_map.cut(src)
        dst_map.save()
        src_map.save()
        print("moved %d nodes  %s -> %s" % (_count(blob), src_map.alias,
                                            dst_map.path_of(dst_map.resolve(dst.id))))


def _count(blob):
    return len(re.findall(rb"<node[ >]", blob))


def cmd_icons(args):
    print("status vocabulary (friendly name -> Freeplane builtin)\n")
    for k, v in ICONS.items():
        print("  %-9s %-3s %s" % (k, MARK.get(v, ""), v))
    print("\nraw Freeplane builtin names are accepted too")


def cmd_stats(args):
    from collections import Counter
    tot = 0
    for m in all_maps():
        ic = Counter(i for n in m.nodes for i in n.icons)
        tot += len(m.nodes)
        print("%-22s %5d nodes  %6.0f KB" % (m.alias, len(m.nodes),
                                             len(m.data) / 1024.0))
        for b, c in ic.most_common(6):
            print("      %-20s %5d  %s" % (BUILTIN2NAME.get(b, b), c, MARK.get(b, "")))
    print("%-22s %5d nodes" % ("TOTAL", tot))
    print("\nfreeplane running: %s" % ("YES - writes blocked" if freeplane_running()
                                       else "no"))


def cmd_session(args):
    if args.action == "list":
        if not os.path.isdir(SESSIONS):
            print("no sessions yet (%s)" % SESSIONS)
            return
        rows = sorted(f for f in os.listdir(SESSIONS) if f.endswith(".mm"))
        if not rows:
            print("no sessions yet")
        for f in rows:
            full = os.path.join(SESSIONS, f)
            try:
                m = Map("session", full)
                print("  %-52s %4d nodes  %s" % (f, len(m.nodes), m.root.label(40)))
            except Exception as e:
                print("  %-52s UNREADABLE (%s)" % (f, e))
        return
    if not args.title:
        die("session new needs a title")
    path = new_session(args.title)
    print("created %s" % path)
    print("open it in Freeplane, write a request, mark it with the '%s' icon, then save"
          % TRIGGER)


HANDLED = os.path.join(SESSIONS, ".mm-handled.json")


def _handled():
    try:
        with open(HANDLED, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_handled(d):
    os.makedirs(SESSIONS, exist_ok=True)
    tmp = HANDLED + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    os.replace(tmp, HANDLED)


def cmd_inbox(args):
    """List launch-marked nodes not yet handled, or mark some handled.

    The on-disk file keeps its launch icon after a turn (the tick we queue lands
    in Freeplane's memory only), so 'handled' has to be tracked outside the map.
    """
    key = os.path.abspath(args.mapfile)
    st = _handled()
    seen = set(st.get(key, []))
    if args.done:
        st[key] = sorted(seen | set(args.done))
        _save_handled(st)
        print("marked handled: %s" % " ".join(args.done))
        return
    m = Map("session", args.mapfile)
    pending = [n for n in m.nodes if TRIGGER in n.icons and n.id not in seen]
    if not pending:
        print("nothing pending")
        return
    for n in pending:
        print("%s  %s" % (n.id, n.label(88)))
        for k in n.kids:
            print("      %s  %s" % (k.id, k.label(80)))


def cmd_post(args):
    """Enqueue a structured (optionally nested) node spec for the live map.

    Reads JSON on stdin: one object, or a list of them. Each object takes
    text / icons / note / children, children being the same shape recursively.
    """
    try:
        spec = json.load(sys.stdin)
    except ValueError as e:
        die("stdin is not valid JSON: %s" % e)
    specs = spec if isinstance(spec, list) else [spec]
    path = os.path.abspath(args.mapfile)

    def clean(d):
        out = {"text": str(d.get("text", "")),
               "icons": [icon_builtin(i) for i in (d.get("icons") or [])]}
        if d.get("note"):
            out["note"] = str(d["note"])
        if d.get("color"):
            out["color"] = str(d["color"])
        if d.get("children"):
            out["children"] = [clean(c) for c in d["children"]]
        return out

    for d in specs:
        node = clean(d)
        enqueue(dict({"op": "add", "map": path, "target": args.target}, **node))
        print("queued: %s%s" % (node["text"][:70],
                                "  (+%d children)" % len(node.get("children", []))
                                if node.get("children") else ""))


def cmd_watch(args):
    armed, info = watcher_state()
    print("watcher   : %s" % ("ARMED" if armed else "not armed"))
    if info:
        age = time.time() - info.get("ts", 0) / 1000.0
        print("heartbeat : %.1fs ago%s" % (age, "" if armed else "  (stale)"))
        print("applied   : %s   failed: %s" % (info.get("applied"), info.get("failed")))
        print("last      : %s" % info.get("last"))
    else:
        print("status    : %s not found" % STATUS)
    pending = 0
    if os.path.exists(QUEUE):
        with open(QUEUE, encoding="utf-8") as fh:
            pending = sum(1 for l in fh if l.strip())
    print("queued    : %d command(s) in %s" % (pending, QUEUE))
    if not armed:
        print("\narm it in Freeplane: Tools > MM Watch > Start map watcher")


def cmd_check(args):
    ok = True
    for alias, fn in MAPS.items():
        path = os.path.join(HERE, fn)
        try:
            m = Map(alias, path)
            print("ok    %-22s %5d nodes" % (alias, len(m.nodes)))
        except Exception as e:
            ok = False
            print("FAIL  %-22s %s" % (alias, e))
    sys.exit(0 if ok else 1)


# ---------------- export / import: Markdown as the shared, recoverable form ----------------
#
# The map is the master; the Markdown is what the team reads in the repo and what the
# map can be rebuilt from if the .mm file is lost.  Format (one node per line):
#
#   # root text                          H1 = map root, H2.. = the first --headings levels
#   ## ✓ branch <!-- ID_12 #d35400 -->   icon tokens first, hidden metadata in a comment
#   > note paragraph                     blockquote lines under a node are its note
#   - ⚠ [text](url)                      deeper levels are nested bullets, 2 spaces/level
#     - child <!-- {icon:list} -->       an icon with no glyph is written as {icon:NAME}
#
# Dropped on purpose: fold state, fonts, CREATED/MODIFIED, layout attributes.  Kept:
# text, hierarchy, icons, notes (as plain paragraphs), links, colour; node ids and
# arrow links with --ids.  `mm.py roundtrip` measures exactly what a map would lose.

MARK2BUILTIN = {}
for _b, _s in MARK.items():
    MARK2BUILTIN.setdefault(_s, _b)      # first wins: "✓" is button_ok, not yes
_ICON_TOKENS = sorted(MARK2BUILTIN, key=len, reverse=True)
_HEAD_RE = re.compile(r"^(#{1,6})(?:\s+(.*))?$")
_BULLET_RE = re.compile(r"^(\s*)[-*+](?:\s+(.*))?$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_COMMENT_RE = re.compile(r"\s*<!--\s*(.*?)\s*-->")
_LINK_RE = re.compile(r"^\[(.*)\]\((\S+)\)$", re.S)


def _own_body(m, n):
    """A node's XML between its start tag and its first child (or its end)."""
    if n.selfclose:
        return ""
    hi = n.kids[0].start if n.kids else n.end - len(b"</node>")
    body = m.data[n.inner:hi].decode("utf-8", "replace")
    return re.sub(r"<hook\b[^>]*/>|<hook\b.*?</hook>", "", body, flags=re.S)


def _rich_paragraphs(xml):
    """Plain-text paragraphs of a Freeplane richcontent block."""
    t = re.search(r"<text>(.*?)</text>", xml, re.S)
    if t:
        return [l.rstrip() for l in html.unescape(t.group(1)).split("\n")]
    body = re.search(r"<body>(.*?)</body>", xml, re.S)
    body = body.group(1) if body else xml
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    paras = re.split(r"</?(?:p|li|h[1-6]|div|ul|ol)\b[^>]*>", body, flags=re.I)
    out = []
    for p in paras:
        p = html.unescape(re.sub(r"<[^>]+>", "", p))
        lines = [" ".join(l.split()) for l in p.split("\n")]
        lines = [l for l in lines if l]
        if lines:
            out.extend(lines)
    return out


def node_export_text(m, n):
    if n.text is not None:
        return n.text
    mt = re.search(r'<richcontent TYPE="NODE".*?</richcontent>', _own_body(m, n), re.S)
    return "\n".join(_rich_paragraphs(mt.group())) if mt else ""


def node_note_lines(m, n):
    """Own note (and details, which Markdown cannot tell apart) as paragraphs."""
    out = []
    for kind in ("NOTE", "DETAILS"):
        for mt in re.finditer(r'<richcontent TYPE="%s"[^>]*?(?:/>|>.*?</richcontent>)' % kind,
                              _own_body(m, n), re.S):
            out.extend(_rich_paragraphs(mt.group()))
    return out


def node_color(m, n):
    mt = re.search(rb'\sCOLOR="([^"]*)"', m.data[n.start:n.inner])
    return mt.group(1).decode() if mt else None


def node_arrows(m, n):
    return re.findall(r'<arrowlink [^>]*?DESTINATION="(ID_\d+)"', _own_body(m, n))


def export_markdown(m, root, headings=2, ids=False, exclude=()):
    """Render a subtree as Markdown; returns (text, stats)."""
    skip = set(id(x) for x in exclude)
    used_icons, stats = {}, {"nodes": 0, "notes": 0, "excluded": len(exclude)}
    out = []

    def line_for(n):
        toks = []
        for i in n.icons:
            used_icons[i] = used_icons.get(i, 0) + 1
            toks.append(MARK.get(i) or "{icon:%s}" % i)
        text = "<br>".join(l.strip() for l in node_export_text(m, n).split("\n")).strip()
        if n.link:
            text = "[%s](%s)" % (text, n.link)
        meta = []
        if ids and n.id:
            meta.append(n.id)
        c = node_color(m, n)
        if c:
            meta.append(c)
        if ids:
            meta.extend("->%s" % d for d in node_arrows(m, n))
        s = " ".join(toks + [text]) if text or toks else ""
        return s + (" <!-- %s -->" % " ".join(meta) if meta else "")

    def walk(n, depth):
        if id(n) in skip:
            return
        stats["nodes"] += 1
        rel = depth
        note = node_note_lines(m, n)
        if rel <= headings:
            if out:
                out.append("")
            out.append("#" * (rel + 1) + " " + line_for(n))
            for l in note:
                out.append("> " + l if l else ">")
            if note:
                out.append("")
        else:
            pad = "  " * (rel - headings - 1)
            out.append(pad + "- " + line_for(n))
            for l in note:
                out.append(pad + "  > " + l if l else pad + "  >")
        if note:
            stats["notes"] += 1
        for k in n.kids:
            walk(k, depth + 1)

    walk(root, 0)
    legend = " · ".join("%s %s" % (MARK.get(i, "{icon:%s}" % i), BUILTIN2NAME.get(i, i))
                        for i in sorted(used_icons, key=lambda i: -used_icons[i]))
    head = ["<!-- Generated by `mm.py export` from %s on %s." % (m.path_of(root),
                                                                   time.strftime("%Y-%m-%d")),
            "     The mind map is the master: edits made here are overwritten by the next",
            "     export. Rebuild the map from this file with `mm.py import FILE --to NEW.mm`."]
    if legend:
        head.append("     Icons: %s." % legend)
    if exclude:
        head.append("     Not exported: %s." % "; ".join(x.label(40) for x in exclude))
    head[-1] += " -->"
    return "\n".join(head + out) + "\n", stats


def parse_markdown(text):
    """Markdown (as written by export_markdown) -> nested dicts.  Lenient: any
    bullet style, 2- or 4-space indents, tabs, stray paragraphs become notes."""
    root, stack, last, in_comment = None, [], None, False
    heading_depth, indent_unit = -1, None

    def make(s):
        n = {"text": "", "icons": [], "link": None, "note": [], "kids": [],
             "id": None, "color": None, "arrows": []}
        for c in _COMMENT_RE.findall(s):
            for tok in c.split():
                if re.fullmatch(r"ID_\d+", tok):
                    n["id"] = tok
                elif re.fullmatch(r"#[0-9a-fA-F]{6}", tok):
                    n["color"] = tok
                elif re.fullmatch(r"->ID_\d+", tok):
                    n["arrows"].append(tok[2:])
        s = _COMMENT_RE.sub("", s).strip()
        while True:
            mt = re.match(r"\{icon:([^}]+)\}(?:\s+|$)", s)
            if mt:
                n["icons"].append(mt.group(1))
                s = s[mt.end():]
                continue
            for tok in _ICON_TOKENS:
                if s == tok or s.startswith(tok + " "):
                    n["icons"].append(MARK2BUILTIN[tok])
                    s = s[len(tok):].lstrip()
                    break
            else:
                break
        mt = _LINK_RE.match(s)
        if mt:
            s, n["link"] = mt.group(1), mt.group(2)
        n["text"] = re.sub(r"<br\s*/?>", "\n", s, flags=re.I).strip()
        return n

    def attach(depth, n, lineno):
        nonlocal root, last
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if not stack:
            if root is not None:
                die("line %d: a second top-level node (%r); a map has exactly one root - "
                    "the H1 (or the single top bullet)" % (lineno, n["text"][:40]))
            root = n
        else:
            stack[-1][1]["kids"].append(n)
        stack.append((depth, n))
        last = n

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        s = line.strip()
        if in_comment:
            in_comment = "-->" not in s
            continue
        if not s:
            continue
        if s.startswith("<!--"):
            in_comment = "-->" not in s
            continue
        mt = _HEAD_RE.match(line)
        if mt:
            heading_depth = len(mt.group(1)) - 1
            attach(heading_depth, make(mt.group(2) or ""), lineno)
            continue
        mt = _BULLET_RE.match(line)
        if mt:
            ind = len(mt.group(1).expandtabs(2))
            if ind and indent_unit is None:
                indent_unit = ind
            depth = heading_depth + 1 + (ind // (indent_unit or 2))
            attach(depth, make(mt.group(2) or ""), lineno)
            continue
        mt = _QUOTE_RE.match(line)
        if last is not None:
            last["note"].append(mt.group(1) if mt else s)
    if root is None:
        die("no nodes found in the Markdown")
    return root


def build_map_xml(root, style=b""):
    """Nested dicts -> a complete Freeplane .mm document (bytes)."""
    now = int(time.time() * 1000)
    known, counter = set(), [0]

    def collect(n):
        if n["id"]:
            if n["id"] in known:
                n["id"] = None          # duplicate ids in the file: reassign
            else:
                known.add(n["id"])
        for k in n["kids"]:
            collect(k)

    def assign(n):
        while not n["id"]:
            counter[0] += 1
            cand = "ID_%d" % counter[0]
            if cand not in known:
                known.add(cand)
                n["id"] = cand
        for k in n["kids"]:
            assign(k)

    collect(root)
    assign(root)
    dropped = []

    def emit(n, out, depth):
        attrs = 'TEXT="%s" ID="%s" CREATED="%d" MODIFIED="%d"' % (esc(n["text"]), n["id"],
                                                                 now, now)
        if depth == 0:
            attrs += ' FOLDED="false" STYLE="oval"'
        if n["color"]:
            attrs += ' COLOR="%s"' % n["color"]
        if n["link"]:
            attrs += ' LINK="%s"' % esc(n["link"])
        out.append("<node %s>" % attrs)
        if depth == 0:
            out.append('<font SIZE="18"/>')
            if style:
                out.append(style.decode("utf-8"))
        for i in n["icons"]:
            out.append('<icon BUILTIN="%s"/>' % esc(i))
        for d in n["arrows"]:
            if d in known:
                out.append('<arrowlink DESTINATION="%s"/>' % d)
            else:
                dropped.append((n["id"], d))
        if n["note"]:
            out.append('<richcontent TYPE="NOTE" CONTENT-TYPE="xml/">\n<html>\n  <head>\n  '
                       '</head>\n  <body>\n' +
                       "".join("    <p>\n      %s\n    </p>\n" % esc_text(l or "")
                               for l in n["note"]) + '  </body>\n</html>\n</richcontent>')
        for k in n["kids"]:
            emit(k, out, depth + 1)
        out.append("</node>")

    out = ['<map version="freeplane 1.12.15">',
           '<!--To view this file, download free mind mapping software Freeplane from '
           'https://www.freeplane.org -->']
    emit(root, out, 0)
    out.append("</map>")
    data = ("\n".join(out) + "\n").encode("utf-8")
    expat.ParserCreate().Parse(data, True)          # never emit broken XML
    return data, dropped


def fingerprints(m, root, exclude=()):
    """(depth, text, icons, note, link, color) per node in tree order - what the
    Markdown form promises to preserve."""
    skip = set(id(x) for x in exclude)
    out = []

    def walk(n, d):
        if id(n) in skip:
            return
        out.append((d, " ".join(node_export_text(m, n).split()), tuple(n.icons),
                    tuple(node_note_lines(m, n)), n.link, node_color(m, n)))
        for k in n.kids:
            walk(k, d + 1)

    walk(root, 0)
    return out


def _resolve_excludes(m, root, specs):
    """--exclude values are paths under the exported node, or ids anywhere in the map."""
    return [m.resolve(s, start=root) for s in specs or []]


def cmd_export(args):
    m, n = parse_target(args.target or ((DEFAULT_MAP or "") + ":"))
    exclude = _resolve_excludes(m, n, args.exclude)
    text, st = export_markdown(m, n, headings=max(0, min(args.headings, 5)), ids=args.ids,
                               exclude=exclude)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("wrote %s: %d nodes, %d notes%s" % (
            args.output, st["nodes"], st["notes"],
            (", %d branch(es) excluded" % st["excluded"]) if st["excluded"] else ""))
    else:
        sys.stdout.write(text)


def cmd_import(args):
    if os.path.exists(args.to) and not args.overwrite:
        die("%s exists; pass --overwrite to replace it" % args.to)
    if os.path.exists(args.to) and freeplane_running() and not args.force:
        die("Freeplane is running and might have %s open; close it or pass --force"
            % args.to)
    text = open(args.mdfile, encoding="utf-8").read()
    root = parse_markdown(text)
    data, dropped = build_map_xml(root, style_hooks(required=False))
    tmp = args.to + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, args.to)
    m = Map(os.path.basename(args.to), args.to)
    notes = sum(1 for x in m.nodes if node_note_lines(m, x))
    print("wrote %s: %d nodes, %d with icons, %d notes" % (
        args.to, len(m.nodes), sum(1 for x in m.nodes if x.icons), notes))
    for src, dst in dropped:
        print("  dropped arrow %s -> %s (target not in this file)" % (src, dst))
    if not any(re.search(r"<!--[^>]*\bID_\d+", l) for l in text.splitlines()):
        print("  note: the file carried no node ids (export without --ids); ids are fresh, "
              "so any alias:ID_… references to the old map no longer resolve")


def cmd_roundtrip(args):
    """Export, re-import into a scratch file, and diff the fingerprints."""
    m, n = parse_target(args.target or ((DEFAULT_MAP or "") + ":"))
    exclude = _resolve_excludes(m, n, args.exclude)
    text, st = export_markdown(m, n, headings=args.headings, ids=True, exclude=exclude)
    data, dropped = build_map_xml(parse_markdown(text), b"")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".mm", delete=False) as fh:
        fh.write(data)
        tmp = fh.name
    try:
        m2 = Map("roundtrip", tmp)
        a, b = fingerprints(m, n, exclude), fingerprints(m2, m2.root)
    finally:
        os.unlink(tmp)
    names = ("depth", "text", "icons", "note", "link", "color")
    losses = 0
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            losses += 1
            if losses <= args.limit:
                print("node %d %r:" % (i, x[1][:50]))
                for nm, p, q in zip(names, x, y):
                    if p != q:
                        print("    %-6s map: %r\n    %-6s md : %r" % (nm, p, "", q))
    if len(a) != len(b):
        losses += abs(len(a) - len(b))
        print("node count differs: map %d, re-import %d" % (len(a), len(b)))
    for src, dst in dropped:
        print("arrow %s -> %s would be dropped (points outside the exported subtree)"
              % (src, dst))
    print("%s: %d nodes compared, %d difference(s)%s" % (
        m.path_of(n), len(a), losses,
        " - lossless for text/hierarchy/icons/notes/links/colour" if not losses else ""))
    sys.exit(1 if losses else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("tree", help="show structure")
    p.add_argument("target", nargs="?")
    p.add_argument("-d", "--depth", type=int, default=2)
    p.set_defaults(fn=cmd_tree)

    p = sub.add_parser("find", help="search node text")
    p.add_argument("pattern")
    p.add_argument("-i", "--icon")
    p.add_argument("-n", "--limit", type=int, default=40)
    p.add_argument("-m", "--map", choices=list(MAPS))
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("show", help="node detail")
    p.add_argument("target")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("add", help="append a child node")
    p.add_argument("parent")
    p.add_argument("text")
    p.add_argument("--icon", action="append")
    p.add_argument("--note")
    p.add_argument("--link")
    p.add_argument("--color", help='node text color, #rrggbb')
    p.add_argument("--first", action="store_true")
    p.add_argument("--live", action="store_true",
                   help="hand the edit to the Freeplane watcher instead of the file")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("color", help="set node text color")
    p.add_argument("target")
    p.add_argument("color", help="#rrggbb hex value")
    p.add_argument("--live", action="store_true",
                   help="hand the edit to the Freeplane watcher instead of the file")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_color)

    p = sub.add_parser("icon", help="set status icons")
    p.add_argument("target")
    p.add_argument("--set", action="append", default=[])
    p.add_argument("--add", action="append", default=[])
    p.add_argument("--clear", action="store_true")
    p.add_argument("--live", action="store_true",
                   help="hand the edit to the Freeplane watcher instead of the file")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_icon)

    p = sub.add_parser("note", help="attach a note")
    p.add_argument("target")
    p.add_argument("text")
    p.add_argument("--live", action="store_true",
                   help="hand the edit to the Freeplane watcher instead of the file")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser("rename", help="change node text")
    p.add_argument("target")
    p.add_argument("text")
    p.add_argument("--live", action="store_true",
                   help="hand the edit to the Freeplane watcher instead of the file")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_rename)

    p = sub.add_parser("rm", help="delete a subtree")
    p.add_argument("target")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_rm)

    p = sub.add_parser("mv", help="move a subtree")
    p.add_argument("target")
    p.add_argument("--to", required=True)
    p.add_argument("--first", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_mv)

    sub.add_parser("icons", help="list status vocabulary").set_defaults(fn=cmd_icons)
    p = sub.add_parser("session", help="create a Claude session map")
    p.add_argument("action", choices=["new", "list"])
    p.add_argument("title", nargs="?")
    p.set_defaults(fn=cmd_session)

    p = sub.add_parser("inbox", help="pending launch-marked nodes in a session map")
    p.add_argument("mapfile")
    p.add_argument("--done", nargs="*", help="mark these node ids handled")
    p.set_defaults(fn=cmd_inbox)

    p = sub.add_parser("post", help="enqueue a nested node spec (JSON on stdin)")
    p.add_argument("mapfile")
    p.add_argument("target")
    p.set_defaults(fn=cmd_post)

    sub.add_parser("watch", help="live-watcher status").set_defaults(fn=cmd_watch)
    sub.add_parser("stats", help="counts per map").set_defaults(fn=cmd_stats)
    sub.add_parser("check", help="validate all maps").set_defaults(fn=cmd_check)

    p = sub.add_parser("export", help="render a map or subtree as Markdown")
    p.add_argument("target", nargs="?", help="alias:path or id; default: the default map")
    p.add_argument("-o", "--output", help="file to write (default: stdout)")
    p.add_argument("--headings", type=int, default=2,
                   help="levels below the root rendered as headings, deeper = bullets (2)")
    p.add_argument("--ids", action="store_true",
                   help="keep node ids and arrow links in hidden comments (for recovery)")
    p.add_argument("--exclude", action="append", metavar="PATH",
                   help="subtree (path under target, or id) to leave out; repeatable")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("import", help="rebuild a map from exported Markdown")
    p.add_argument("mdfile")
    p.add_argument("--to", required=True, metavar="NEW.mm", help="map file to create")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("roundtrip", help="what would export+import lose? (exit 1 if anything)")
    p.add_argument("target", nargs="?")
    p.add_argument("--headings", type=int, default=2)
    p.add_argument("--exclude", action="append", metavar="PATH")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.set_defaults(fn=cmd_roundtrip)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
