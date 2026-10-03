# alarm-recipient-scan

What a Niagara station does with an alarm when the far end is down - and why
returning `false` from `sendAlarm` loses the alarm instead of retrying it. Read
out of `alarm-rt.jar` and `baja.jar` rather than out of the documentation.

One Python file, standard library only.

```
./alarm-recipient-scan.py [NIAGARA_HOME]
```

## Why this exists

Anyone who turns Niagara alarms into something outside Niagara - a work order, a
ticket, a message, a webhook - subclasses
`javax.baja.alarm.BRecoverableRecipient` and implements one method:

```java
protected abstract boolean sendAlarm(BAlarmRecord) throws Exception
```

Two words in that signature decide what happens on a bad day: the `boolean` and
the `throws`. They do not mean what they look like they mean.

Returning `false` reads like "not sent, please retry". In the bytecode it is a
bare `return`: the record is not queued, the status is not set to fault,
`lastFailureCause` is not written, the retry thread is not started, and nothing
is logged. The alarm is gone and the station still looks healthy. **Throwing is
the only way into the recovery machinery** - the exception handler is what sets
fault status, persists the record and starts the retry loop.

That matters a second time over, because there is no give-up and no dead letter.
A record the far end will never accept is retried on a 15-second default for as
long as the station runs, and with `persistent=true` each retry is one file
delete and one file write on a controller's flash.

And it matters a third time, because almost none of it reaches the log. Of the
seven logging sites in the class, six are FINE - off by default - and only the
persistence failure is a WARNING. The evidence a site can actually send you
lives in the recipient's own properties: `status`, `lastFailureTime`,
`lastFailureCause`, `queuedAlarmCount`. A product that does not surface those
leaves an operator with nothing to look at.

## What it reads, and where from

- `BRecoverableRecipient`'s static initialiser - the real defaults for
  `retryInterval` and `persistent`, with their facets.
- `handleAlarm`'s bytecode - where `sendAlarm` is called, how its return value
  is tested, and the exception handler ranges that make up the recovery path,
  in order.
- The private `RetryThread` - the sleep clamp, what `poll()` chooses between
  disk and memory, how `dequeueMemory` snapshots the size, and the condition
  under which the thread kills itself.
- `javax.baja.util.Queue` - the default bound of the no-argument constructor,
  so "the queue is bounded" can be printed as the number it actually is.
- The class's own string constants, so the log messages listed are the ones the
  jar contains.

## Read first: what it does and does not touch

**It never connects to a station.** It unzips jars out of a Niagara installation
and runs `javap`. Nothing is installed, patched, written to a station or sent
anywhere.

It needs a Niagara installation to read and a `javap` from a JDK 8. It looks for
`javap` in `$JAVAP`, then on `PATH`, then under `$JAVA_HOME` and the Niagara
install, then in Debian's default location. The install to read comes from the
first argument or `$NIAGARA_HOME`.

**The output below was measured against Niagara 4.15.5.22.** Another version may
differ, and that is the point - run it against yours rather than trusting this
page.

## Running it

```
$ ./alarm-recipient-scan.py
Alarm recipients: what happens when the far end is down
======================================================================

read from /opt/Niagara/Niagara-4.15.5.22
javap:    1.8.0_504

The one method a vendor implements:
  protected abstract boolean sendAlarm(javax.baja.alarm.BAlarmRecord) throws java.lang.Exception;

Property defaults, read out of the static initialiser, not the docs:
  retryInterval  15000 ms (15 s), with a 'min' facet of 1000 ms
  persistent     true
  so out of the box a failed alarm is written to disk and retried
  every 15 seconds, for as long as the station runs.

Step 1. handleAlarm calls sendAlarm on the alarm thread, at offset 82.
        The return value is tested once:
          ifne 91   -> true: set lastSendTime, status ok
          90: return  <- false: RETURN, and nothing else at all

  So returning false does not mean 'not sent, please retry'. It means
  'forget this alarm'. The record is not queued, the status is not set
  to fault, lastFailureCause is not written, the retry thread is not
  started, and nothing is logged. The alarm is gone, and the station
  looks healthy.

Step 2. A thrown Exception is the only way into the recovery machinery.
        The handler (ranges 40-90, 91-145) does, in order:
          status := fault, lastFailureTime := now,
          lastFailureCause := the exception's toString (its cause, if
          it is a BajaRuntimeException),
          then if persistent: write <uuid>.xml under the recipient's
          persistence directory, inside AccessController.doPrivileged,
          and set queuedAlarmCount from a listing of that directory;
          else: Queue.enqueue the record in memory,
          then start the RetryThread if it is not already running.

Step 3. The in-memory queue is the no-argument javax.baja.util.Queue
        (the recipient calls "<init>":()V), whose default bound is
        2,147,483,647 - Integer.MAX_VALUE.
        public synchronized boolean enqueue(java.lang.Object) throws javax.baja.util.QueueFullException;
        throws only at that bound, and handleAlarm does not catch it.
        On a JACE the real bound is the heap, not a setting.

Step 4. The retry loop, in a thread named 'alarm:RecipRetryThread':
          sleep(Math.max(retryInterval, 1000 ms)) - so a retryInterval
          under 1 s is clamped up to 1 s - then poll().
          poll(): persistent and queuedAlarmCount > 0 -> dequeueDisk,
          otherwise dequeueMemory. When queuedAlarmCount reaches 0 the
          thread kills itself and the field is nulled, so the loop
          exists exactly as long as there is something to retry.
          dequeueMemory takes a size snapshot, dequeues that many and
          calls handleAlarm on each, so a record that fails again is
          re-queued at the tail: the retry order is not the alarm order.
          dequeueDisk lists the directory, sorts it with a comparator,
          and for each file decodes the record, deletes the file, then
          calls handleAlarm - which re-writes the same file on failure.

Step 5. What reaches the station log: 6 FINE sites and 1 WARNING.
        The messages in the class are:
          RecoverableRecipient: Failed to delete queue file
          RecoverableRecipient: failed @
          RecoverableRecipient: failed to persist alarm
          RecoverableRecipient: handleAlarm
          RecoverableRecipient: sending ...
          RecoverableRecipient: sent @
        Only the persistence failure is a WARNING. Every send failure,
        every retry and every poll error is FINE, which is off by
        default - so the evidence lives in the recipient's own
        properties (status, lastFailureTime, lastFailureCause,
        queuedAlarmCount), not in the log a site will send you.

Three consequences worth designing for
----------------------------------------------------------------------
  1. return false only for 'this alarm is not mine'. For a failure,
     throw. Returning false on a timeout loses alarms silently.
  2. There is no give-up and no dead letter. A record the far end will
     never accept is retried every 15 s for as long as the station
     runs. With persistent=true that is one delete and one write per
     retry - about 11520 a day - on a controller's flash.
  3. queuedAlarmCount and lastFailureCause are the only operator-
     visible evidence. A product that does not surface them leaves a
     site with nothing to look at.

Three checks one station settles in an afternoon
----------------------------------------------------------------------
  1. Point the recipient at a dead endpoint, raise one alarm, and read
     queuedAlarmCount: zero means your sendAlarm returned false and the
     alarm is gone; one means it threw and the alarm is queued.
  2. Leave it failing for ten minutes and list the persistence
     directory: one <uuid>.xml per queued alarm, rewritten every 15 s.
  3. Bring the endpoint back and watch the order the records arrive in.
```

## The same finding, written up

Why returning `false` from `sendAlarm` loses the alarm while throwing retries it - and what the recovery machinery does with the record either way - is also written up as a page: <https://plantroomlabs.com/tools/alarm-recipient-scan/>. It carries a captured run of this program, the download with its byte count and SHA-256, the Niagara version the bytecode was read on beside the version of the JACE it was checked against, and the note on alarm routing that puts it next to the queue behaviour.

## Licence

MIT. Written by Usama Iqbal at [Plantroom Labs](https://plantroomlabs.com).
