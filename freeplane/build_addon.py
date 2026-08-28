#!/usr/bin/env python3
"""Package mm-watch.groovy as an installable Freeplane add-on.

Run after editing mm-watch.groovy:

    python3 freeplane/build_addon.py

Then in Freeplane: Tools > Add-ons... > install from file > mmwatch.addon.mm

The add-on carries its own permissions, so nothing global has to be loosened -
only this add-on's script gets file read/write, and exec/network stay denied.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "mm-watch.groovy")
OUT = os.path.join(HERE, "mmwatch.addon.mm")

NAME = "mmwatch"
VERSION = "v0.2.0"
AUTHOR = "Sergei Veselovskii"
FP_FROM = "v1.12.0"
SCRIPT = "mm-watch.groovy"

# Only what the watcher actually does: read the queue file, write the status
# file. No subprocesses, no network.
PERMISSIONS = {
    "execute_scripts_without_asking": "true",
    "execute_scripts_without_file_restriction": "true",
    "execute_scripts_without_write_restriction": "true",
    "execute_scripts_without_exec_restriction": "false",
    "execute_scripts_without_network_restriction": "false",
}

DESCRIPTION = (
    "Applies edits queued by mm.py to the open map, so Freeplane no longer has "
    "to be closed before editing a map from the command line. Arm it once per "
    "session; it stops when Freeplane exits. It never saves the map - edits "
    "arrive as unsaved changes."
)

LICENSE = (
    "This add-on is free software: you can redistribute it and/or modify it "
    "under the terms of the GNU General Public License as published by the "
    "Free Software Foundation, either version 2 of the License, or (at your "
    "option) any later version."
)

CT = 1785484000000
_seq = [700000000]


def nid():
    _seq[0] += 101
    return "ID_%d" % _seq[0]


def esc(s):
    """Escape for an XML attribute exactly the way Freeplane writes them."""
    s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
          .replace('"', "&quot;"))
    return (s.replace("\r\n", "\n").replace("\r", "\n")
             .replace("\n", "&#xa;").replace("\t", "&#x9;"))


def node(text, children=(), attrs=()):
    out = ['<node TEXT="%s" ID="%s" CREATED="%d" MODIFIED="%d">'
           % (esc(text), nid(), CT, CT)]
    for k, v in attrs:
        out.append('<attribute NAME="%s" VALUE="%s"/>' % (esc(k), esc(v)))
    out.extend(children)
    out.append("</node>")
    return "".join(out)


def main():
    if not os.path.exists(SRC):
        sys.exit("missing %s" % SRC)
    code = open(SRC, encoding="utf-8").read()

    # Keep the payload ASCII: the add-on installer round-trips this text through
    # XML and Freeplane's own writer, and non-ASCII would depend on charset luck.
    bad = [(i + 1, l) for i, l in enumerate(code.splitlines())
           if any(ord(ch) > 126 for ch in l)]
    if bad:
        for ln, l in bad[:5]:
            print("non-ascii at line %d: %s" % (ln, l.strip()[:70]), file=sys.stderr)
        sys.exit("mm-watch.groovy must be pure ASCII")

    # menuLocation takes NO leading slash for main-menu entries. Freeplane's own
    # keys are 'main_menu_scripting' and 'main_menu_scripting/scripts'; a leading
    # slash fails to resolve and the menu item is dropped without any warning.
    script_node = node(SCRIPT, children=[node(code)], attrs=[
        ("menuTitleKey", "addons.${name}.start"),
        ("menuLocation", "main_menu_scripting/addons.${name}"),
        ("executionMode", "on_single_node"),
        # fallback entry point, in case the menu placement misbehaves again
        ("keyboardShortcut", "control alt shift W"),
    ] + sorted(PERMISSIONS.items()))

    body = node(
        "MM Watch",
        attrs=[("name", NAME), ("version", VERSION), ("author", AUTHOR),
               ("freeplaneVersionFrom", FP_FROM), ("freeplaneVersionTo", ""),
               ("updateUrl", "")],
        children=[
            node("description", [node(DESCRIPTION)]),
            node("changes", [node(VERSION, [node("initial release")])]),
            node("license", [node(LICENSE)]),
            node("preferences.xml"),
            node("default.properties"),
            node("translations", [node("en", attrs=[
                ("addons.${name}", "MM Watch"),
                ("addons.${name}.start", "Start map watcher"),
            ])]),
            node("deinstall", attrs=[
                ("delete", "${installationbase}/addons/${name}.script.xml"),
                ("delete", "${installationbase}/addons/${name}/scripts/" + SCRIPT),
            ]),
            node("scripts", [script_node]),
            node("lib"),
            node("zips"),
            node("images"),
        ])

    xml = ('<map version="freeplane 1.12.15">\n'
           '<!--To view this file, download free mind mapping software Freeplane '
           'from https://www.freeplane.org -->\n' + body + "\n</map>\n")
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(xml)

    # never ship something that will not parse
    import xml.etree.ElementTree as ET
    root = ET.parse(OUT).getroot()
    n = len(list(root.iter("node")))
    print("wrote %s (%d bytes, %d nodes)" % (OUT, os.path.getsize(OUT), n))
    print("permissions granted to %s:" % SCRIPT)
    for k, v in sorted(PERMISSIONS.items()):
        print("   %-45s %s" % (k, v))


if __name__ == "__main__":
    main()
