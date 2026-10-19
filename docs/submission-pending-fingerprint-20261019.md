# Pending submission observations and unchanged fingerprints

A pending SQLite mapping observation could remain cached when a later commit did not change the observed database size/mtime fingerprint. The signal is advisory and does not establish physical vector integrity, but retaining a pending result could prevent timely exact verification when a completion event was absent.

Pending observations now repeat the existing bounded, read-only mapping query. Completed observations retain their unchanged-fingerprint fast path. Workspace ownership, complete-record requirements, the 25 ms SQLite timeout, missing-database behavior and exact physical verification remain unchanged.

An intermittent full-suite failure was consistent with this mechanism; the original failure did not record the fingerprint. A deterministic control holding the observed fingerprint constant while committing the missing record reproduced the same failure before the patch and passed after it. The original commit-wakeup assertion was retained. Eight ingestion-observation tests passed, including completed-result query reuse, ownership, busy storage and missing database controls. The subsequent full deterministic suite passed 3266 tests with 1 skipped, 34 deselected and 28 subtests. This is isolated SQLite/control coverage, not a live upload or embedding qualification.
