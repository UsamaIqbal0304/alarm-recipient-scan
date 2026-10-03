#!/usr/bin/env python3
"""What a Niagara station does with an alarm when the far end is down.

    tools/alarm-recipient-scan.py [NIAGARA_HOME]

Why this exists. Anyone who turns Niagara alarms into something outside
Niagara - a work order, a ticket, a message, a webhook - writes a subclass of
javax.baja.alarm.BRecoverableRecipient and implements one method:

    protected abstract boolean sendAlarm(BAlarmRecord) throws Exception

Two words in that signature decide what happens to an alarm on a bad day:
the boolean and the throws. They do not mean what they look like they mean,
and the difference is the difference between a retry queue and a lost alarm.
This script reads the answer out of alarm-rt.jar and baja.jar rather than
asserting it: property defaults out of the class's static initialiser, the
control flow out of handleAlarm's bytecode, the queue's bound out of
javax.baja.util.Queue, and the retry loop out of the private RetryThread.

Nothing is installed, patched or sent anywhere. unzip and javap only.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

def _find_javap(niagara_home=None):
    """javap from $JAVAP, then PATH, then the JDK Niagara ships, then Debian's."""
    cand = [os.environ.get("JAVAP"), shutil.which("javap")]
    for base in (os.environ.get("JAVA_HOME"), niagara_home):
        if base:
            cand += [os.path.join(base, "bin", "javap"),
                     os.path.join(base, "jre", "bin", "javap")]
    cand.append("/usr/lib/jvm/java-8-openjdk-amd64/bin/javap")
    for c in cand:
        if c and os.path.exists(c):
            return c
    return None


HOME = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/Niagara/Niagara-4.15.5.22")
JAVAP = _find_javap(str(HOME))


def abort(msg):
    print("ABORT " + msg, file=sys.stderr)
    sys.exit(2)


def dis(classpath, cls):
    out = subprocess.run([JAVAP, "-p", "-c", "-classpath", str(classpath), cls],
                         capture_output=True, text=True)
    if out.returncode != 0 or "Compiled from" not in out.stdout:
        abort("javap failed on %s: %s" % (cls, out.stderr.strip()[:200]))
    return out.stdout


def method(text, sig, what):
    """The bytecode of one method: from its signature to the blank line."""
    i = text.find(sig)
    if i < 0:
        abort("no %s in this jar (looked for %r)" % (what, sig))
    j = text.find("\n\n", i)
    return text[i:j if j > 0 else len(text)]


def one(hay, pat, what):
    m = re.findall(pat, hay)
    if len(m) != 1:
        abort("%d matches for %s (expected 1)" % (len(m), what))
    return m[0]


if not Path(JAVAP).exists():
    abort("no javap at %s" % JAVAP)
for jar in ("alarm-rt.jar", "baja.jar"):
    if not (HOME / "modules" / jar).exists():
        abort("no %s under %s" % (jar, HOME / "modules"))

work = Path(tempfile.mkdtemp(prefix="alarm-scan-"))
for jar in ("alarm-rt.jar", "baja.jar"):
    with zipfile.ZipFile(HOME / "modules" / jar) as z:
        z.extractall(work)

ver = subprocess.run([JAVAP, "-version"], capture_output=True, text=True).stdout.strip()
RR = dis(work, "javax.baja.alarm.BRecoverableRecipient")
RT = dis(work, "javax.baja.alarm.BRecoverableRecipient$RetryThread")
Q = dis(work, "javax.baja.util.Queue")

print("Alarm recipients: what happens when the far end is down")
print("=" * 70)
print()
print("read from %s" % HOME)
print("javap:    %s" % ver)
print()

# ---- 1. the contract -------------------------------------------------------
ABS = one(RR, r"(protected abstract boolean sendAlarm\(javax\.baja\.alarm\.BAlarmRecord\)"
              r" throws java\.lang\.Exception;)", "the sendAlarm signature")
print("The one method a vendor implements:")
print("  %s" % ABS)
print()

# ---- 2. property defaults, out of the static initialiser ------------------
static = method(RR, "  static {};", "the static initialiser")
iv = re.search(r"ldc2_w\s+#\d+\s+// long (\d+)l\n\s+\d+: invokestatic\s+#\d+\s+"
               r"// Method javax/baja/sys/BRelTime.make:\(J\)Ljavax/baja/sys/BRelTime;\n"
               r"\s+\d+: ldc\s+#\d+\s+// String min\n"
               r"\s+\d+: ldc2_w\s+#\d+\s+// long (\d+)l", static)
if not iv:
    abort("could not read the retryInterval default out of the static block")
retry_ms, retry_min_ms = int(iv.group(1)), int(iv.group(2))

pers = re.search(r"(iconst_[01])\n\s+\d+: aconst_null\n\s+\d+: invokestatic\s+#\d+\s+"
                 r"// Method newProperty:\(IZLjavax/baja/sys/BFacets;\)"
                 r"Ljavax/baja/sys/Property;\n\s+\d+: putstatic\s+#\d+\s+"
                 r"// Field persistent:Ljavax/baja/sys/Property;", static)
if not pers:
    abort("could not read the persistent default out of the static block")
persistent_default = pers.group(1) == "iconst_1"

print("Property defaults, read out of the static initialiser, not the docs:")
print("  retryInterval  %d ms (%g s), with a 'min' facet of %d ms"
      % (retry_ms, retry_ms / 1000.0, retry_min_ms))
print("  persistent     %s" % str(persistent_default).lower())
print("  so out of the box a failed alarm is written to disk and retried")
print("  every %g seconds, for as long as the station runs." % (retry_ms / 1000.0))
print()

# ---- 3. handleAlarm: the boolean --------------------------------------------
ha = method(RR, "  public void handleAlarm(javax.baja.alarm.BAlarmRecord);",
            "handleAlarm")
send_at = one(ha, r"\n\s+(\d+): invokevirtual\s+#\d+\s+// Method sendAlarm:"
                  r"\(Ljavax/baja/alarm/BAlarmRecord;\)Z", "the sendAlarm call")
tail = ha[ha.index("%s: invokevirtual" % send_at):]
false_branch = re.search(r"istore_2\n\s+(\d+): iload_2\n\s+\d+: ifne\s+(\d+)\n"
                         r"\s+(\d+): return", tail)
if not false_branch:
    abort("handleAlarm does not have the shape this script knows how to read")
ok_target, false_return = false_branch.group(2), false_branch.group(3)

print("Step 1. handleAlarm calls sendAlarm on the alarm thread, at offset %s."
      % send_at)
print("        The return value is tested once:")
print("          ifne %s   -> true: set lastSendTime, status ok" % ok_target)
print("          %s: return  <- false: RETURN, and nothing else at all"
      % false_return)
print()
print("  So returning false does not mean 'not sent, please retry'. It means")
print("  'forget this alarm'. The record is not queued, the status is not set")
print("  to fault, lastFailureCause is not written, the retry thread is not")
print("  started, and nothing is logged. The alarm is gone, and the station")
print("  looks healthy.")
print()

# ---- 4. handleAlarm: the throws --------------------------------------------
exc = re.findall(r"\n\s+(\d+)\s+(\d+)\s+(\d+)\s+Class java/lang/Exception", ha)
if not exc:
    abort("handleAlarm has no catch of java/lang/Exception")
for s in ("Field javax/baja/status/BStatus.fault", "Method setLastFailureTime",
          "Method setLastFailureCause", "Method getPersistent:()Z",
          "Method javax/baja/alarm/BAlarmRecord.getUuid:()Ljavax/baja/util/BUuid;",
          "Method javax/baja/util/Queue.enqueue:(Ljava/lang/Object;)Z",
          "class javax/baja/alarm/BRecoverableRecipient$RetryThread"):
    if s not in ha:
        abort("handleAlarm's recovery path is missing %r" % s)
xml = one(ha, r'// String (\.xml)', "the persisted file suffix")

print("Step 2. A thrown Exception is the only way into the recovery machinery.")
print("        The handler (ranges %s) does, in order:"
      % ", ".join("%s-%s" % (a, b) for a, b, _ in exc))
print("          status := fault, lastFailureTime := now,")
print("          lastFailureCause := the exception's toString (its cause, if")
print("          it is a BajaRuntimeException),")
print("          then if persistent: write <uuid>%s under the recipient's" % xml)
print("          persistence directory, inside AccessController.doPrivileged,")
print("          and set queuedAlarmCount from a listing of that directory;")
print("          else: Queue.enqueue the record in memory,")
print("          then start the RetryThread if it is not already running.")
print()

# ---- 5. the queue's bound ---------------------------------------------------
qctor = method(Q, "  public javax.baja.util.Queue();", "Queue's no-arg constructor")
qmax = int(one(qctor, r"// int (\d+)", "Queue's default maxSize"))
qsig = one(Q, r"(public synchronized boolean enqueue\(java\.lang\.Object\)"
              r" throws javax\.baja\.util\.QueueFullException;)", "enqueue")
rrq = one(RR, r"// Method javax/baja/util/Queue.\"<init>\":\((\w*)\)V",
          "the Queue constructor the recipient calls")
print("Step 3. The in-memory queue is the no-argument javax.baja.util.Queue")
print("        (the recipient calls \"<init>\":(%s)V), whose default bound is"
      % rrq)
print("        %s - Integer.MAX_VALUE." % f"{qmax:,}")
print("        %s" % qsig)
print("        throws only at that bound, and handleAlarm does not catch it.")
print("        On a JACE the real bound is the heap, not a setting.")
print()

# ---- 6. the retry loop ------------------------------------------------------
tname = one(RT, r'// String (alarm:\w+)', "the retry thread's name")
run = method(RT, "  public void run();", "RetryThread.run")
floor_ms = int(one(run, r"// long (\d+)l", "the sleep floor"))
if "Method java/lang/Math.max:(JJ)J" not in run or "Method sleep:(J)V" not in run:
    abort("RetryThread.run is not the sleep/poll loop this script knows")
poll = method(RT, "  private void poll();", "RetryThread.poll")
for s in ("Method dequeueDisk:()V", "Method dequeueMemory:()V", "Method kill:()V"):
    if s not in poll:
        abort("poll() is missing %r" % s)
dmem = method(RT, "  private void dequeueMemory()", "dequeueMemory")
ddisk = method(RT, "  private void dequeueDisk()", "dequeueDisk")
if "Method java/util/Arrays.sort:([Ljava/lang/Object;Ljava/util/Comparator;)V" not in ddisk:
    abort("dequeueDisk does not sort its listing")
for name, blk in (("dequeueMemory", dmem), ("dequeueDisk", ddisk)):
    if "Method javax/baja/alarm/BRecoverableRecipient.handleAlarm:" not in blk:
        abort("%s does not call back into handleAlarm" % name)

print("Step 4. The retry loop, in a thread named %r:" % tname)
print("          sleep(Math.max(retryInterval, %d ms)) - so a retryInterval"
      % floor_ms)
print("          under %g s is clamped up to %g s - then poll()."
      % (floor_ms / 1000.0, floor_ms / 1000.0))
print("          poll(): persistent and queuedAlarmCount > 0 -> dequeueDisk,")
print("          otherwise dequeueMemory. When queuedAlarmCount reaches 0 the")
print("          thread kills itself and the field is nulled, so the loop")
print("          exists exactly as long as there is something to retry.")
print("          dequeueMemory takes a size snapshot, dequeues that many and")
print("          calls handleAlarm on each, so a record that fails again is")
print("          re-queued at the tail: the retry order is not the alarm order.")
print("          dequeueDisk lists the directory, sorts it with a comparator,")
print("          and for each file decodes the record, deletes the file, then")
print("          calls handleAlarm - which re-writes the same file on failure.")
print()

# ---- 7. what the station tells you ----------------------------------------
fine = len(re.findall(r"Field java/util/logging/Level.FINE", RR))
warn = len(re.findall(r"Field java/util/logging/Level.WARNING", RR))
warn_msgs = sorted(set(re.findall(r"// String (RecoverableRecipient: [^\n]*)", RR)))
print("Step 5. What reaches the station log: %d FINE sites and %d WARNING."
      % (fine, warn))
print("        The messages in the class are:")
for m in warn_msgs:
    print("          %s" % m.strip())
print("        Only the persistence failure is a WARNING. Every send failure,")
print("        every retry and every poll error is FINE, which is off by")
print("        default - so the evidence lives in the recipient's own")
print("        properties (status, lastFailureTime, lastFailureCause,")
print("        queuedAlarmCount), not in the log a site will send you.")
print()

print("Three consequences worth designing for")
print("-" * 70)
print("  1. return false only for 'this alarm is not mine'. For a failure,")
print("     throw. Returning false on a timeout loses alarms silently.")
print("  2. There is no give-up and no dead letter. A record the far end will")
print("     never accept is retried every %g s for as long as the station"
      % (retry_ms / 1000.0))
print("     runs. With persistent=%s that is one delete and one write per"
      % str(persistent_default).lower())
print("     retry - about %d a day - on a controller's flash."
      % int(2 * 86400 / (retry_ms / 1000.0)))
print("  3. queuedAlarmCount and lastFailureCause are the only operator-")
print("     visible evidence. A product that does not surface them leaves a")
print("     site with nothing to look at.")
print()
print("Three checks one station settles in an afternoon")
print("-" * 70)
print("  1. Point the recipient at a dead endpoint, raise one alarm, and read")
print("     queuedAlarmCount: zero means your sendAlarm returned false and the")
print("     alarm is gone; one means it threw and the alarm is queued.")
print("  2. Leave it failing for ten minutes and list the persistence")
print("     directory: one <uuid>%s per queued alarm, rewritten every %g s."
      % (xml, retry_ms / 1000.0))
print("  3. Bring the endpoint back and watch the order the records arrive in.")
