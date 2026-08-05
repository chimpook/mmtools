# The MM Watch add-on — internals and troubleshooting

Read this when the live watcher misbehaves, or before changing `mm-watch.groovy`.
For day-to-day usage see the "Live mode" section of `CLAUDE.md`.

## How it fits together

```
mm.py --live  ──append (flock)──>  ~/work/.mm-queue.jsonl
                                          │
                                    (locked drain + truncate, 400ms poll)
                                          v
   Freeplane ── MM Watch add-on ── daemon thread ── SwingUtilities.invokeAndWait
                                          │
                                          v
                          open in-memory map (unsaved changes)

                          ~/work/.mm-watch.status  <── heartbeat every ~3s
```

Freeplane stays the **only** process that ever writes the `.mm` file, which is what
removes the conflict rather than merely managing it. The watcher deliberately does not
call `save()`: edits land as unsaved changes so undo keeps working and the user decides
when to write them out.

| File | Role |
| --- | --- |
| `freeplane/mm-watch.groovy` | the watcher — source of truth, edit this |
| `freeplane/build_addon.py` | packages it into the installable add-on |
| `freeplane/mmwatch.addon.mm` | generated; install this in Freeplane |
| `~/.config/freeplane/1.12.x/addons/mmwatch.script.xml` | installed descriptor |
| `~/.config/freeplane/1.12.x/addons/mmwatch/scripts/mm-watch.groovy` | installed script |

## Changing the watcher

```bash
$EDITOR freeplane/mm-watch.groovy
python3 freeplane/build_addon.py          # rebuild; refuses non-ASCII, validates XML
cp freeplane/mm-watch.groovy ~/.config/freeplane/1.12.x/addons/mmwatch/scripts/
```
Copying the script over the installed copy means a plain Freeplane restart picks up the
change — no reinstall. Reinstall only if the descriptor (permissions, menu, name)
changed.

The payload must be **pure ASCII**: it round-trips through XML attribute encoding and
Freeplane's own writer, and non-ASCII would depend on charset luck. `build_addon.py`
enforces this.

## Permissions

Granted to this add-on's script alone; nothing global is loosened:

```
execute_scripts_without_asking               true
execute_scripts_without_file_restriction     true    # read the queue
execute_scripts_without_write_restriction    true    # write the status file
execute_scripts_without_exec_restriction     false
execute_scripts_without_network_restriction  false
```

The add-on does **not** hold `RuntimePermission "manageProcess"`. Anything like
`ProcessHandle.current().pid()` throws `AccessControlException` — this cost an
afternoon once, because the real work succeeded while only the status write failed,
making `mm.py watch` report "not armed" on a perfectly functional watcher.

## Troubleshooting

**The watcher reports in the Freeplane log**, not to stdout. That is the first place
to look:
```bash
grep "mm-watch" ~/.config/freeplane/1.12.x/logs/log.0
```
Expect `mm-watch: armed, watching …` then one `OK`/`SKIP`/`ERR` line per command.

- **`mm.py watch` says "not armed"** — most likely it simply is not armed: arming is a
  menu action and the daemon thread dies with Freeplane, so every restart needs another
  click. Confirm against the log before assuming a bug.
- **`SKIP map not open`** — the target map is not open in Freeplane. The watcher scans
  `c.openMaps` by canonical path, so it does not matter which tab has focus, but the map
  does have to be open.
- **Menu item missing** — the **Add-ons dialog shows each add-on's real menu path in its
  description**. Check there before hunting through menus or reading bytecode. MM Watch
  lands directly under **Tools**, not under Tools → Scripts, because
  `menuLocation=main_menu_scripting/addons.${name}`.
- **`menuLocation` takes no leading slash** for main-menu entries. Freeplane's keys are
  `main_menu_scripting` and `main_menu_scripting/scripts`; `/main_menu_...` fails to
  resolve and the item is dropped with no warning anywhere.
- **Install fails with "Missing properties: [name, version, author,
  freeplaneVersionFrom]"** — the add-on map must be the **open, focused map with a node
  selected**. `installScriptAddOn.groovy` reads properties from `node.map.root`, so with
  any other map current it inspects that map's root and finds nothing. The add-on file
  is fine.
- **Fallback trigger** if the menu is uncooperative: **Ctrl+Alt+Shift+W**.

## Do not try to use signed scripts

Freeplane's signed-script mechanism is broken on this setup, established by
disassembling `SignedScriptHandler`:

- `isScriptSigned` (verification) calls `initializeKeystore(**null**)`
- `signScript` prompts via `EnterPasswordDialog` and passes a real password
- both use `KeyStore.getDefaultType()`, which is **pkcs12** on Freeplane's Java 21
- PKCS12 loaded with a null password exposes **no entries**, so `getCertificate()`
  always returns null and verification always fails, **silently**

Two further traps if anyone retries: PKCS12 lowercases aliases, so a mixed-case
`FreeplaneScriptKey` never resolves; and Java 21 rejects `SHA1withDSA` for 2048-bit
keys. The feature was written for the JKS era, where `load(in, null)` still gave read
access to certificates.

## Offline verification

Freeplane ships Groovy 4, so the whole toolchain can be checked without launching the
app — worth doing before asking anyone to restart:

```bash
GJ=/usr/share/freeplane/plugins/org.freeplane.plugin.script/lib
FP=/usr/share/freeplane
CP="$GJ/groovy-4.0.27.jar:$GJ/groovy-json-4.0.27.jar:$GJ/groovy-cli-picocli-4.0.27.jar:\
$GJ/picocli-4.7.7.jar:$FP/freeplanelauncher.jar:\
$FP/core/org.freeplane.core/lib/freeplaneviewer.jar:\
$FP/plugins/org.freeplane.plugin.script/lib/plugin-1.13.2.jar"
/usr/lib/jvm/java-21-openjdk/bin/java -cp "$CP" groovy.ui.GroovyMain -e '
  new GroovyShell(this.class.classLoader).parse(new File("freeplane/mm-watch.groovy").text)
  println "compiles"'
```
Also worth asserting after every rebuild: the script embedded in the `.addon.mm` is
byte-identical to `freeplane/mm-watch.groovy`. The ASM jars in that same directory
allow disassembling Freeplane's own classes when documentation runs out.

Note Freeplane runs on **java-21-openjdk** (not the system default 26), and its active
config directory is **`1.12.x`** even though the app is 1.13.2 — the `1.13.x` directory
exists only to document that decision.
