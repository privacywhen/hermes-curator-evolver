# Evidence privacy and strict inspection

## Defaults and threat model

Curator Evolver sees tool arguments, outputs, and conversation turns. Those can
contain credentials, private project prose, commands, paths, or entire documents.
A length limit is not a privacy boundary. Credential-shaped redaction cannot find
all private content, and local collection is not permission to upload evidence.

New collection is **metadata-only by default**: timestamps, session/task/tool and
skill identifiers, duration, outcome/error flags, model/platform identifiers, and
aggregate counts. Skill attribution arguments are allowlisted; arbitrary command
arguments, outputs, turn bodies, and queue metadata payloads are omitted. Existing
evidence is not migrated, rewritten, expired, or deleted. Legacy rows are minimized
and sanitized when read/published. Identifiers and reviewed candidate titles can
still reveal operational context; metadata is not anonymous.

For deliberate local diagnostic collection, `EvidenceStore(collect_previews=True)`
or the exact process setting `HERMES_CURATOR_EVOLVER_COLLECT_PREVIEWS=1` opts into
sanitized collection only. Other values do not opt in. This setting applies to
the launched process; select the intended active
profile first. It is not a setting to add to Hermes's credential `.env` file.
Unsetting it stops subsequent content collection, not historical storage.
Do not enable it for sessions whose arbitrary prose must remain uncollected.

Stored rows do not carry collection-time consent provenance. Collection settings
therefore never authorize reading their old prose. Reports, automatic notes,
spillover references, and queue reads remain metadata-only even when collection
is enabled. For a deliberate **local** diagnostic disclosure, use
`report --include-legacy-previews` or `analyze --include-legacy-previews` (native
commands also accept these flags). They include bounded sanitized stored content,
including legacy rows; the JSON privacy disclosure records this read consent.
Python callers must explicitly set `EvidenceStore(include_legacy_previews=True)`
or `ReviewQueue(..., create=False, include_legacy_previews=True)` for that reader.
This is current read consent, not a claim of historical collection consent.
It does not authorize provider/model egress, rewriting, or deletion. CLI queue
lists and automatic publication have no implicit content opt-in.

Previews default to 500 characters, capped at 2,000; zero means no preview.
Credential sanitization precedes slicing/serialization. Structured sensitive keys,
common GitHub/provider/cloud token shapes, Authorization headers, labeled tokens,
and PEM private-key blocks are covered by synthetic tests. Encoded, novel,
fragmented, and unlabeled secrets and ordinary PII can escape detection; benign
text may be over-redacted. Replacement counts mean matched replacement operations,
not unique secrets or a completeness score. Detection has no persistent counters.

Publication boundaries re-sanitize legacy report rows, proposal prompts/outputs,
ranking inputs, managed evidence notes, spillover references, and queue lists.
Required sanitization failures stop that content path; hook failures log exception
classes rather than raw payloads. Metadata-only status/count inspection remains
available. No added provider calls, background service, index, policy engine, or
persistent database is required.

Sanitization is best-effort, not a parser sandbox: arbitrary deeply nested,
cyclic, or oversized opt-in payloads have no dedicated traversal resource budget
and may be refused by Python/serialization limits. Do not opt into unbounded
untrusted content. A failed content path is not proof that evidence was collected.

## What read-only inspection means

Path resolution does not create directories. Inspection opens existing evidence
only; missing, corrupt, or old-schema sources return failures without repair or
quarantine. Writer construction remains explicit and retains initialization and
legacy corrupt-file quarantine behavior. Connection contexts close handles on
exit, including on Windows.

A SQLite `mode=ro` connection can create/change WAL/SHM artifacts. Instead, strict
inspection copies at most **64 MiB into memory**, checks source metadata around
that copy, and deserializes a query-only connection. It never connects SQLite to
the source path or spills private database copies to disk. It refuses any
WAL/SHM/journal companion, WAL-format header, source drift, oversize source, or
SQLite build without `deserialize`. This is **not** a live backup protocol: supply
a quiescent, closed, consistent DELETE-mode snapshot. Do not delete journals or
change a live database's journal mode to make the check pass. Preparing a snapshot
is a separately controlled operator operation; inspection does not do it.

`status`, `report`, `analyze`, proposal/verification preparation, queue listing,
default dry-run `auto-run`, and a restore drill's evidence probe use this boundary.
A restore drill itself intentionally writes its disposable restoration/report/state
outputs; queue mining, ingestion, apply/rollback, bootstrap, and scheduler commands
are not read-only inspection commands. Pure path helpers have explicit
`create=True` writer options where needed.

CLI success is 0; handled I/O/SQLite/validation errors, refusal, failed verifier,
failed apply/rollback/drill, model-unavailable result, and partial/missing backfill
are nonzero. A warning-only/partial drill remains 0 with its warning status visible;
it is not a fully passing restore gate. Native registered handlers and standalone
`python -m hermes_curator_evolver` return the same integer result. Positive day
windows are required; `--limit 0` for candidates/pages intentionally returns none
without loading models, and negative limits fail.

## Bounded review and retention inventory

`candidates-list --queue-db PATH --limit 100 --offset 0 --format json` provides
`pagination.limit`, `offset`, `returned`, and `has_more` using one-row lookahead.
Page sizes are 0..100; there is no invented total. Library list access is also
bounded. Evidence report samples are bounded (20 tool rows, 10 turns); report
windows filter retrieval, not retention.

`python -m hermes_curator_evolver privacy-inventory --retention-days 30 --file-limit 100`
counts rows and age ranges in a strict inspection snapshot, and inventories at most
1,000 backup-directory entries/1,000 files without reading backup content or
following symlinks/junctions. Truncation is explicit. It has **no deletion action**.
Age is not approval to purge. Evidence, journals, backups, manifests, and support
files may be interdependent; legacy backups can retain raw credentials indefinitely.
Reverting code must not restore a raw-data snapshot as a privacy rollback.

## Remaining execution boundaries

Sanitizing a prompt is not a sandbox or consent to transmit it. The existing
optional `hermes chat` drafting subprocess remains prompt-constrained, not hard
no-tool constrained; fixing that mechanism and explicit provider-egress consent
belongs to P03. Do not use real evidence drafting pending that gate. Semantic
model downloads/execution, LLWM compatibility, installed activation, native hook
delivery, scheduler wiring, and live skill changes need separate runtime evidence
and authorization. Synthetic tests and resolver dry runs do not prove those gates.

Hygiene patterns/fixtures were selectively adapted from MIT-licensed
`codeo1io/hermes-curator-evolver` at
`c5aac56a6a902bff3d56a16cb0cfca88c31e45e3`, particularly commits
`27e94c84d81a7f0162a1b90161aacb22e8b78c65`,
`fe6610ac269e71bd7fceae6207bbc7857fdb807b`, and
`2de240b3fb5f4492648444c79c0dc0e70ea7b694`. No sibling storage architecture,
retry/WAL manager, dedupe migration, or autonomy stack was imported.
