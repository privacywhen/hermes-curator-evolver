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

## Event provenance and partial results

Available native tool-call/turn/request identifiers and status are operational
metadata, not consent to retain prose or proof of later usefulness. Ordinary short
identifiers remain available; recognized-sensitive, oversized (over 128 characters),
and reserved digest-marker identifiers use full-input SHA-256 fingerprints rather
than shared redaction or truncated prefixes for identity. Internal turn replay keys
also separate event, turn, message and positional namespaces. Readers minimize
pre-existing identifier values without rewriting the database. Fingerprints preserve
correlation, not anonymity or reversibility; unknown secret formats can still escape
best-effort detection. Live turns without supplied identity cannot be reliably
matched to historical positional turns; no text-based identity is invented.

Missing or unknown outcome provenance stays unknown; a stored non-error flag alone does not
prove success. Batch actions remain distinct from parent invocations and cannot
be positively successful after contradictory/rolled-back results.

Import failures expose exception classes rather than private exception messages.
Source failures, failed sessions and failed event writes are separate counters;
partial success retains earlier imported counts and produces a nonzero CLI result.
Legacy records lacking native identifiers are not retrospectively assigned native
provenance, and identity/replay guarantees depend on the identifiers actually
available. No evidence is automatically deleted or rewritten to resolve ambiguity.

## Remaining execution boundaries

Sanitizing a prompt is not consent to transmit it. Optional drafting now binds the
supported host-owned `ctx.llm.complete` interface; there is no nested chat process,
agent tool loop, inherited conversation, provider override, or subprocess fallback.
The default is deterministic with zero model calls (the host API is not even
resolved without consent). Native `hermes curator-evolver propose --draft-with-model`
explicitly consents to transmitting bounded sanitized skill identity/text, counts,
and evidence rows to the host active route and configured fallback. Payload classes
and routing policy are printed to stderr **before** the call and recorded in output;
the exact destination is host-resolved at call time. CLI proposal evidence remains
metadata-only; local legacy-preview read consent never enables drafting.

The user payload is capped at 12,000 characters plus a fixed system instruction;
skill text contributes at most 4,000 characters. Host output is requested at 1,024
tokens and timeout 1–300 seconds (default 180). Non-string, empty, or over-16,000-
character replies fail closed; displayed sanitized draft text is capped at 4,000.
Requested/attempted/response-received/completed stages and returned provider/model
are recorded; exception detail is omitted because it can echo private evidence.
Tool-call-shaped text is inert draft data. Structural verification/human approval
remain separate; no model semantic verification or apply permission is implied.
An absent/older host API or standalone model request returns nonzero with its
deterministic proposal intact. Standalone deterministic proposals remain available.
These boundaries are covered by fake callers, not live-provider acceptance. Semantic
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
