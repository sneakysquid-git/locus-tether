# Development History

The GitLab issue tracker this project was originally built against won't
migrate cleanly to GitHub — comments, discussion threads, and cross-links
mostly don't survive an automated import. This document exists so the real
story doesn't disappear along with it: what actually broke, what actually
got fixed, and what was learned along the way. Not a changelog — a
narrative, written after the fact, of how this project actually came
together.

## The starting point

The goal was a genuinely local alternative to the Omi wearable's own
companion app — one with zero cloud service dependency, not just
relocated compute. Omi's own "self-hosted" backend option still leans on
Firebase, Pinecone, Redis, and Typesense even when self-hosted; the whole
point here was to have none of that. Whisper for transcription, a local
LLM for analysis, nothing phoning home.

The hardware — an NVIDIA Jetson Thor — was new enough that basic pieces
didn't have prebuilt support yet anywhere. That turned out to define the
whole first phase of work.

## Fighting the hardware before writing any real features

**CTranslate2** (the inference engine behind fast local Whisper
transcription) had no prebuilt wheel for this GPU architecture at all — it
had to be compiled from source, on-device, the first real hurdle before
anything else could work.

**Ollama** silently fell back to CPU-only inference on this hardware,
because its own install script didn't recognize the JetPack version and
had no error message to explain why generation was suddenly extremely
slow. Fixed with a manual systemd override forcing GPU use explicitly.

**MPS (NVIDIA's per-GPU process-sharing tool)** seemed like the right
choice to leave headroom for other workloads sharing the same GPU — until
testing revealed it caused Ollama's model runner to hang indefinitely, a
real driver-level bug on this hardware, not a configuration mistake.
Deliberately disabled; default CUDA time-slicing turned out to be fine
given how brief and bursty the actual GPU usage is.

**Speaker diarization** needed three separate, real rounds of dependency
debugging before it actually worked:
1. PyTorch's official wheels installed cleanly via a bare `pip install
   torch` and correctly detected CUDA — genuinely good news, no
   from-source fight needed here, unlike CTranslate2.
2. Installing the diarization libraries afterward silently downgraded
   torch to an older pinned version as a transitive dependency — and that
   older version had no CUDA wheel for this architecture, so it fell back
   to CPU with no error at all. Fixed by force-reinstalling the correct
   torch version with `--no-deps` after the other installs.
3. A constructor parameter used by the diarization library had been
   renamed in a newer release than what the documentation examples
   assumed — a small thing, but exactly the kind of drift that silently
   breaks tutorials.

None of this was exotic misconfiguration. It was what it actually costs to
be an early adopter of very new hardware.

## Analysis quality: a real hallucination, caught twice

Early testing against a real meeting recording, benchmarked against a
known-good commercial transcription tool, surfaced a specific and
repeatable hallucination pattern: the analysis model would occasionally
repurpose a number from one context into a fabricated fact in another —
turning something like "100% coverage" into an invented claim about "100
containers." A stricter prompt reduced how often this happened, and a
lightweight automated check (comparing whether a number in an extracted
fact could actually be found, in the same shape, in the source transcript)
was added specifically to catch it — logging-only at first, out of
caution about false positives on legitimately-phrased facts.

The exact same pattern then recurred on a second, unrelated real
recording. At that point, with real repeated evidence and zero false
positives observed, the check was upgraded from logging to actually
filtering the fact out. A good example of choosing caution first, then
tightening once there's real evidence a stricter response is justified.

## Voice recognition: a genuine root-cause hunt

Voice enrollment needed to work across a container boundary — the
transcription/diarization pipeline runs inside Docker, while the
lightweight web dashboard runs natively on the host. Storing an enrolled
speaker's profile so *both* sides could see it surfaced two distinct real
bugs, not one:

1. A file-permissions issue — the container created files as root, and
   the host-side process couldn't write to them. Fixed with an explicit
   `umask` in the container entrypoint.
2. A deeper, easily-missed bug: the actual storage path was computed from
   a base directory that resolves *differently* depending on which side of
   the container boundary code runs on. The container-side write appeared
   to succeed — but it was silently writing to a path that only existed
   inside the container's own ephemeral filesystem, invisible to the host
   process trying to read it back. Fixed by moving storage to a directory
   that's genuinely shared and bind-mounted on both sides.

The lesson that stuck: a permissions fix that appears to work can mask a
completely different, more fundamental bug sitting right behind it.
Confirming the *actual* file existed with the *actual* expected content,
rather than just confirming no error was thrown, is what caught the real
issue.

## A subtle accuracy bug found through real use, not testing

Once voice recognition could isolate a specific speaker's own segments
from a multi-person recording (so that speaking-style coaching would
analyze just one person's speech, not blended metrics from everyone in
the room), a new and non-obvious failure mode appeared: pause-detection.
Measuring the gap between two of one speaker's own consecutive segments,
after removing everyone else's segments from the transcript, sometimes
produced enormous "pauses" — one real case flagged a 229-second gap as
something to work on.

The actual cause: that whole 229 seconds was someone *else* talking, not
silence. Filtering to one speaker's own segments had thrown away the
information needed to tell "this person went quiet" apart from "this
person was listening while someone else had a long turn." The fix
required checking the *original*, unfiltered timeline to see whether
another speaker's segment actually fell inside a given gap before counting
it as a real pause. A good example of a bug that was invisible in any unit
test using synthetic data, and only showed up once real, messy,
multi-person conversation data was actually run through the feature it
affected.

## The same bug, found three separate times

A chronological-sort bug — conversations tied on their sort key
(same-day items, sorted only by date with no time-of-day precision) and
silently fell back to alphabetical-by-filename order — was fixed once,
reported as still broken, investigated again, and found to be the exact
same root cause living in two *other*, independently-implemented loader
functions that had never been touched by the first fix. Worth remembering
whenever a bug's root cause seems well understood: check whether the same
mistake was made more than once elsewhere in the codebase before declaring
it resolved.

## Getting ready for other people to actually use this

Once the core pipeline worked reliably, a distinct phase of work focused
on things a solo self-hoster testing their own code never has to think
about, but a stranger installing this for the first time absolutely does:

- **A "reprocess" button** in the dashboard — every round of testing
  throughout this whole project had relied on manually re-copying a file
  into a watched folder to force reprocessing. Fine when you have shell
  access and already understand the pipeline; a real barrier otherwise.
- **A data export** — everything valuable lived in JSON files on one
  device with no built-in way to get a copy out.
- **A system status panel** — Ollama reachability, disk space, whether the
  processing container is actually running — so a real problem shows up as
  a specific, checkable fact instead of just "it isn't working."
- **A genuine first-run experience** — distinguishing "nothing happened
  today" (normal) from "nothing has ever been processed, ever" (a brand
  new install that needs actual onboarding, not just a day's silence).

None of these were requested by name before they were built — they came
from deliberately thinking about the gap between "I have been debugging
this live the entire time it was built" and "a complete stranger's very
first five minutes."

## Documentation, and the value of checking your own work

Writing the public-facing documentation surfaced its own small round of
real mistakes, caught only by actually looking rather than assuming: an
architecture diagram whose background color override silently didn't
apply on the actual rendering platform, discovered only from a real
screenshot, not from re-reading the diagram's source; two edge labels in
that same diagram overlapping unreadably, from routing both edges through
a shared generic boundary point instead of the specific components that
actually produced them; and stale references to a feature (a third-party
export integration) that had already been removed from the code, still
lingering in multiple separate places in the prose describing it.

None of these were caught by re-reading the documentation carefully.
They were caught by actually rendering the diagram and comparing it to
what was intended, and by grep-ing the whole document for a removed
feature's name rather than trusting that one cleanup pass had gotten
every mention.

## The throughline

Almost nothing in this list was a design failure caught in review. All of
it was found by actually running things against real data, on real
hardware, and taking a "did this actually do what I think it did" check
seriously even after something appeared to work. Self-hosted, hardware-
adjacent software rewards exactly that kind of paranoia — and this project
hit that lesson more than once.
