# Automatic first-output source evidence

The existing automatic output hook now supplies compact source evidence once per
native session. It uses AB: exact definition snippets and reference locations,
with a shared maximum of 256 locally estimated tokens. Small packets stay small.
Existing output reduction has priority. The source path runs only when the
existing hook would otherwise leave a result unchanged, under its existing Plus
entitlement and mode checks. There is no new tool, flag, prompt, skill or helper
call to obtain it.

The hook derives targets from the first visible model response or tool arguments
in the native transcript. If transcript context is unavailable, it confines reads
to literal source paths already named in the native command. Project inventory
uses Git's ignored-file policy, excludes hidden files, known secret names,
credentials and benchmark/evidence directories, and selects at most 256 source
files. Detected restrictive source instructions cause abstention before source
collection, including restrictions in later user turns. These conservative
checks do not claim to interpret arbitrary natural-language access policies.

Source reads reject symlinks, non-regular files and concurrent changes. Windows
reads additionally verify the final opened handle path before reading bytes.
Selected hashes are checked again before delivery. There is no source byte cap.
Exact entity-number binding avoids similarly numbered identifiers. Already
visible evidence is suppressed, overlapping lines are merged, and packets that
cannot fit the allowance are declined.

The original native output remains an exact prefix, including failing-command
metadata. Normal tool execution and schemas are unchanged. The local delivery
receipt records the packet and source hashes, separately from cost savings.
Repeated delivery of the same result reuses the packet; later results in the
session are left alone. Missing context, unavailable tokenization and hook errors
leave the normal native result available.

The implementation is enabled by default in the existing installed hook path at
the user's explicit request. The earlier study did not qualify broad cost
savings: AB had promising task-specific observations, with single repetitions
and cache confounds. Activation is not a new efficacy claim. No earlier v5
percentage is reported as a measurement of this implementation.

Validation covers local behavioral checks, existing automatic-hook regressions,
and a native-client stock/installed transport test with temporary profiles and
localhost scripted responses. The native test verifies original output, one
execution, the failing exit status, exact source delivery and no recovery calls.
It uses `gpt-6.1-sol` at low effort, with zero paid provider requests. It does not
measure whole-task provider cost or subscription savings. Windows handle access
is implemented but has not been exercised by the Linux-native test.
