// mm-watch.groovy - lets Freeplane stay open while mm.py edits a map.
//
// mm.py --live appends JSON commands to ~/.mm-bridge/queue.jsonl instead of
// rewriting the .mm file. This watcher consumes that queue and applies each
// command to the *open, in-memory* map on the Swing thread, so there is never
// a second writer competing with Freeplane for the file.
//
// Armed from the menu: Tools > Scripts > MM Watch > Start map watcher.
// Runs as a daemon thread, so it dies with Freeplane.
//
// It deliberately does NOT save the map. Edits appear as unsaved changes and
// you decide when to write them out - that keeps undo working and keeps
// Freeplane the only process that ever writes the file.

import groovy.json.JsonSlurper
import groovy.json.JsonOutput
import javax.swing.SwingUtilities
import java.io.RandomAccessFile
import java.nio.channels.FileLock
import org.freeplane.core.util.LogUtils

final String HOME   = System.getProperty('user.home')
// User-level, not per project: every command carries the absolute path of the
// map it targets, so one queue serves every map collection and this script
// needs no configuration. Do not make these project-relative again.
final File BRIDGE   = new File(HOME, '.mm-bridge')
final File QUEUE    = new File(BRIDGE, 'queue.jsonl')
final File STATUS   = new File(BRIDGE, 'status.json')
final long POLL_MS  = 400
final long BEAT_MS  = 3000
final String GUARD  = 'mm.watch.armed'

if ('1' == System.getProperty(GUARD)) {
    ui.informationMessage('mm-watch is already armed in this Freeplane session.')
    return
}

def controller = c          // proxy captured here; only ever touched on the EDT
def slurper = new JsonSlurper()

// ---------------------------------------------------------------- helpers

// Locate an open map by canonical path. Uses openMaps rather than the current
// map so a command lands correctly even when another tab has focus.
def findMap = { String wanted ->
    for (m in controller.openMaps) {
        try {
            File f = m.file
            if (f != null && f.canonicalPath == wanted) return m
        } catch (Throwable ignored) { }
    }
    return null
}

// Create a node and, recursively, any children the command carries.
// mm.py cannot address a node it created live (targets resolve against the file on
// disk), but in here we hold the live Node objects, so nesting is free. This is what
// lets the bridge post an options node with its options underneath in one command.
def build
build = { parent, java.util.Map spec ->
    def kid = parent.createChild(spec.text as String)
    (spec.icons ?: []).each { kid.icons.add(it as String) }
    if (spec.note) kid.note = spec.note as String
    (spec.children ?: []).each { child -> build(kid, child as java.util.Map) }
    return kid
}

// Apply one command. Must run on the EDT. Returns a human-readable result.
def apply = { java.util.Map cmd ->
    if (!cmd.map) return 'ERR no map in command'
    String wanted = new File(cmd.map as String).canonicalPath
    def mp = findMap(wanted)
    if (mp == null) return "SKIP map not open: ${cmd.map}"

    def target = cmd.target ? mp.node(cmd.target as String) : mp.root
    if (target == null) return "ERR node not found: ${cmd.target}"

    switch (cmd.op) {
        case 'add':
            def kid = build(target, cmd)
            return "OK add ${kid.id} under ${target.id}"
        case 'icon':
            if (cmd.clear) target.icons.clear()
            (cmd.icons ?: []).each { target.icons.add(it as String) }
            return "OK icon ${target.id}"
        case 'note':
            target.note = cmd.note as String
            return "OK note ${target.id}"
        case 'text':
            target.text = cmd.text as String
            return "OK text ${target.id}"
        default:
            return "ERR unknown op: ${cmd.op}"
    }
}

// Drain the queue atomically: read everything under an exclusive lock, then
// truncate, so a concurrent mm.py append cannot be lost.
def drain = {
    if (!QUEUE.exists() || QUEUE.length() == 0) return ''
    RandomAccessFile raf = new RandomAccessFile(QUEUE, 'rw')
    FileLock lock = null
    try {
        lock = raf.channel.lock()
        int len = (int) raf.length()
        if (len == 0) return ''
        byte[] buf = new byte[len]
        raf.readFully(buf)
        raf.setLength(0)
        return new String(buf, 'UTF-8')
    } finally {
        if (lock != null) try { lock.release() } catch (Throwable ignored) { }
        raf.close()
    }
}

// No pid here: ProcessHandle.current().pid() needs RuntimePermission
// "manageProcess", which the add-on does not request (and should not). The
// heartbeat timestamp is the liveness signal, so the pid bought nothing.
boolean[] statusBroken = [false]
def writeStatus = { boolean armed, String last, int applied, int failed ->
    try {
        BRIDGE.mkdirs()
        STATUS.setText(JsonOutput.toJson([
                armed  : armed,
                ts     : System.currentTimeMillis(),
                applied: applied,
                failed : failed,
                last   : last ?: '',
        ]), 'UTF-8')
    } catch (Throwable t) {
        // warn once, not every heartbeat - this used to spam the log
        if (!statusBroken[0]) {
            statusBroken[0] = true
            LogUtils.warn('mm-watch: cannot write status (will not repeat): ' + t)
        }
    }
}

// ---------------------------------------------------------------- worker

int[] tally = [0, 0]        // applied, failed
Thread worker = new Thread({
    String last = 'armed'
    long lastBeat = 0
    writeStatus(true, last, 0, 0)
    LogUtils.info('mm-watch: armed, watching ' + QUEUE)
    while (!Thread.currentThread().isInterrupted()) {
        try {
            String payload = drain()
            if (payload) {
                for (String line : payload.readLines()) {
                    line = line.trim()
                    if (!line || line.startsWith('#')) continue
                    def cmd
                    try {
                        cmd = slurper.parseText(line)
                    } catch (Throwable pe) {
                        tally[1]++; last = 'ERR bad json: ' + pe.message
                        LogUtils.warn('mm-watch: ' + last); continue
                    }
                    String[] out = new String[1]
                    SwingUtilities.invokeAndWait({
                        try { out[0] = apply(cmd) }
                        catch (Throwable ae) { out[0] = 'ERR ' + ae }
                    })
                    last = out[0]
                    if (last?.startsWith('OK')) tally[0]++ else tally[1]++
                    LogUtils.info('mm-watch: ' + last)
                }
                writeStatus(true, last, tally[0], tally[1])
                lastBeat = System.currentTimeMillis()
            } else if (System.currentTimeMillis() - lastBeat > BEAT_MS) {
                writeStatus(true, last, tally[0], tally[1])
                lastBeat = System.currentTimeMillis()
            }
        } catch (InterruptedException ie) {
            break
        } catch (Throwable t) {
            LogUtils.warn('mm-watch: loop error: ' + t)
        }
        try { Thread.sleep(POLL_MS) } catch (InterruptedException ie) { break }
    }
    writeStatus(false, 'stopped', tally[0], tally[1])
    LogUtils.info('mm-watch: stopped')
}, 'mm-watch')
worker.daemon = true
worker.start()

System.setProperty(GUARD, '1')
ui.informationMessage("mm-watch armed.\n\nQueue: ${QUEUE}\nStatus: ${STATUS}\n\n" +
        'Edits arrive as unsaved changes - save when you are ready.\n' +
        'Stops automatically when Freeplane exits.')
