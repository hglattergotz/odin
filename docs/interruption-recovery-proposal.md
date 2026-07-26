# Interruption recovery — settled design

Status: **implemented.** Companion to
[`multi-platform-agents-proposal.md`](multi-platform-agents-proposal.md).
Where the build taught us something the design got wrong, §12 records it rather
than quietly rewriting the section above.

## 0. The problem, in one paragraph

A long queue run is interrupted when the agent CLI hits a provider usage limit
mid-task. Odin routes the task to `failed/` and stops, leaving a large amount of
uncommitted work in the tree. To continue, the user must hand-commit the partial
work, hand-move the task file from `failed/` back to `pending/`, wait for the
limit window to reopen, and re-run — and the retrying agent then walks into a
repository full of half-finished work it has no memory of writing. This design
replaces all of that with one command, and gives the retrying agent the context
it needs to finish rather than rewrite.

## 1. Decision log

Ten decisions, all settled. The rest of the document is their consequences.

| # | Decision | Choice |
|---|---|---|
| 1 | Uncommitted work | **Commit it as a WIP commit.** No stash, no discard, no dirty-tree waiver |
| 2 | Invocation | **`odin run` handles it** on a TTY; `odin recover` exists as the explicit verb |
| 3 | Waiting for the reset window | **Offer interactively**, then cycle automatically |
| 4 | Telling the retrying agent | **Full synthesized resumption brief** |
| 5 | Fate of the WIP commit | **Never squashed** — it stays a normal commit in history, carrying git trailers |
| 6 | Audit trail | **New `interrupted/` state dir** + `.recovery.md` sidecar |
| 7 | Repeated interruption | **Keep cycling**; circuit-break on *no progress*, not attempt count |
| 8 | Tasks stranded in `running/` | **Folded in** — same recovery path |
| 9 | Non-TTY runs | **Halt with instructions** (exit 12); `--recover` / `--wait-for-reset` to opt in |
| 10 | Next step | Design doc first, reviewed, then built (see §12) |

### 1.1 The one that changes what Odin is

Decision 1 means **Odin now writes git history.** Today's non-goal reads "Git is
startup-only … It never commits, pushes, merges, or opens PRs." That becomes:
Odin makes exactly one kind of commit — a WIP checkpoint of work an interrupted
run left behind — and only on the recovery path. It still never pushes, merges,
rebases, or opens PRs, and per-task milestone commits remain the target
project's job.

This is worth the exception because it is *automating a step the user already
performs by hand*. Their own history from the incident that prompted this:

```
b28dcf3 Record 080 recovery and the 8/10 milestone position in STATUS
b3c65a3 WIP milestone 080 (TESTS DO NOT BUILD) + clear the backlog
fe9bb8d Milestone 070: alerts that arrive, and a tripwire for silence
```

`b3c65a3` is a hand-written WIP commit, flagged as not building, kept in
history. Decision 1 writes that commit automatically; decision 5 keeps it in
history exactly as the user already does.

The payoff is not just ergonomic: committing makes the tree clean, so **the
existing clean-tree startup check passes unchanged.** There is no waiver, no
snapshot-subset comparison, no staleness rule, no new way for the safety check
to be wrong. The safety model gets simpler, not weaker.

---

## 2. What the evidence says

Design grounded in `~/.odin/metrics/events.jsonl`: **305 task executions**, 279
completed, 20 held, **6 failed**. Every failure:

| date | task | stop_reason | error | turns | wall | cost |
|---|---|---|---|---|---|---|
| 2026-06-08 | `019-canvas-center-mark` | `end_turn` | `None` | 49 | 499s | $3.23 |
| 2026-06-29 | `002-p13-02-cdk-scaffold` | `end_turn` | `'success'` | 3 | 306s | $1.78 |
| 2026-07-24 | `001-author-openapi-spec` | `stop_sequence` | `'success'` | 1 | 3s | $0.00 |
| 2026-07-24 | `002-author-llms-txt-and…` | `stop_sequence` | `'success'` | 1 | 5s | $0.00 |
| 2026-07-25 | `030-current-prices-rea…` | `stop_sequence` | `'success'` | 66 | 562s | $5.67 |
| 2026-07-25 | `080-ui-port` | `stop_sequence` | `'success'` | 85 | 635s | $8.07 |

**(a) `error: "success"` is a bug and also the signal.** From
`backends/claude.py:174`: when Claude Code sets `is_error: true` while `subtype`
is still `"success"`, the error label becomes the literal string `"success"`.
Five of six lifetime failures carry it, and all five are external terminations.
The one without it (`end_turn` / `None`) is the genuine defect — the agent
finished its turn cleanly and emitted no sentinel.

**(b) Two shapes of interruption.** `080-ui-port` ran 85 turns and $8.07 before
being cut off. The 2026-07-24 pair ran 1 turn, 3 seconds, $0.00 — limits hit
before any work happened. Same class, opposite consequences: the first needs a
WIP commit and a detailed brief, the second leaves a clean tree and recovery is
nearly a no-op. Git handling must key off *what is actually in the tree*, never
off the classification.

**(c) Base rate is 2% and skewed 5:1 toward interruption.** Optimizing the
classifier for defect-precision optimizes the rare case. Treat interruption as
the default hypothesis; make the defect path a cheap explicit override.

---

## 3. Command surface

```
odin recover [STEM] [QUEUE_DIR]
             [--dry-run] [--no-wip-commit] [--no-brief] [--force]
             [--wait-for-reset] [--max-wait MINUTES] [--verify-cmd CMD] [--yes]

odin run     … [--recover] [--no-auto-recover]
                [--wait-for-reset] [--max-wait MINUTES] [--allow-dirty]
```

**Why `recover` and not an overload of `resume`.** `resume` means "the human
answered the agent's questions"; it requires human input and runs against a
clean tree by construction. Recovery requires nothing from the human and runs
against a dirty tree by construction. Keeping them separate preserves
`odin resume`'s semantics untouched — a hard constraint — and keeps
recovery-specific behavior out of the held path, where a dirty tree still
deserves a hard stop. `retry` was rejected because the second attempt is a
*continuation*, not a repeat; `continue` collides with "continue the queue,"
which is already just `odin run`.

**Argument resolution.** `STEM` optional: with none given, if exactly one task
is recoverable (in `interrupted/`, or stranded in `running/`), use it; if
several, list them and exit 2. `QUEUE_DIR` uses the existing
`_resolve_queue_arg` convention, so `odin recover go-rewrite` works.

### 3.1 The primary path — `odin run`

```
$ odin run --platform claude

odin: 080-ui-port was interrupted (usage limit, 2026-07-25 21:32).
      8 files changed, +1187/-43 uncommitted.

  → commit WIP, requeue 080-ui-port, resume with context

Recover and continue? [Y/n]
```

One command, one keystroke. `--no-auto-recover` suppresses the offer;
`--recover` answers yes without asking (this is what makes an unattended run
work).

### 3.2 The explicit path — `odin recover`

```
$ odin recover
⏸ 080-ui-port · interrupted · usage limit
   resets at 15:00 America/New_York (in 2h13m)
   8 files changed, +1187/-43

  → commit  wip(odin): interrupted attempt at 080-ui-port
              [Odin-WIP] 8 files, +1187/-43
  → move    interrupted/080-ui-port.md → pending/
  → brief   resumption context merged into task body

Wait 2h13m and continue automatically? [y/N]
```

Answering `n` to the wait offer still performs the recovery and then asks
`continue the queue now? [Y/n]` — recovery and waiting are independent.
`--dry-run` prints this whole plan, including the exact brief text, and writes
nothing.

### 3.3 Non-TTY

```
$ odin run --platform claude < /dev/null

⏸ 080-ui-port · interrupted · usage limit
   resets at 15:00 America/New_York (in 2h13m)
   8 files changed, +1187/-43 uncommitted

Not a TTY — halting (exit 12).
To recover: odin recover go-rewrite
Or run unattended: odin run --recover --wait-for-reset
```

Nothing commits and nothing sleeps in a non-interactive run unless a flag says
so. This governs a *fresh* non-TTY invocation only — an interactive session that
already consented to waiting keeps cycling on its own (§6.3).

---

## 4. Classification

A new backend hook, defaulting to today's behavior so Cursor and Grok are
unaffected until they implement it:

```python
class AgentBackend:
    def classify_failure(self, result: RunResult, stderr: str) -> Failure:
        """Why did this non-successful run fail? Default: DEFECT."""
        return Failure(kind=FailureKind.DEFECT, confidence="probable",
                       reason="unknown")
```

```python
@dataclass(frozen=True)
class Failure:
    kind: FailureKind            # INTERRUPTED | DEFECT
    confidence: str              # "confirmed" | "probable"
    reason: str                  # "usage_limit" | "process_died" | "unknown"
    detail: str = ""             # human-readable line lifted from the output
    resets_at: datetime | None = None
```

`ClaudeBackend.classify_failure` applies three tiers:

1. **Confirmed interruption** — output or stderr matches a known provider-limit
   pattern. Sets `reason="usage_limit"` and extracts `resets_at` when present.
2. **Probable interruption** — the turn did not end on the agent's own terms:
   non-zero exit, **or** `is_error` true, **or** no terminal event at all.
   `reason="unknown"`, `confidence="probable"`.
3. **Defect** — exit 0, terminal event reported no error, and the failure came
   from `protocol.parse` returning `UNPARSEABLE`. The agent finished and broke
   the contract. Routes to `failed/` exactly as today.

Against the six historical failures this yields 5 interrupted / 1 defect,
matching ground truth. Both zero-cost 3-second cases land in tier 1.

**Pattern matching is enrichment, never routing.** Tier 2 catches the whole
class structurally; tier 1 only adds the human-readable reason and the reset
time. An unrecognized limit message still classifies as interrupted with
`reason="unknown"` — and that is asserted by a test, not by a comment. This is
what makes the fragility of message-matching a non-issue.

Known patterns live in one extensible table in `backends/claude.py`:

| form | example | yields |
|---|---|---|
| epoch | `Claude AI usage limit reached\|1753462800` | exact `resets_at` |
| clock + tz | `session limit · resets 3pm (America/New_York)` | `resets_at` in that tz |
| clock, no tz | `limit reached, resets at 15:00` | `resets_at` in local tz |
| bare | `rate_limit_error`, HTTP `429` | no `resets_at` |

**Odin never asks the agent to checkpoint itself before dying.** Two of the six
failures ended in 3 seconds at $0.00 — no model call was available to emit
anything. A checkpoint block only works for interruptions the agent can see
coming, which is the rare kind. Capturing the stream text Odin already receives
(§9.3) achieves most of the same thing and survives SIGKILL.

---

## 5. State and audit trail

```
queue/
├── interrupted/   NNN-slug.md            — the task body, awaiting recovery
│                  NNN-slug.recovery.md   — evidence, attempt log, brief
```

Deliberately mirrors `held/` (`NNN-slug.md` + `NNN-slug.questions.md`), so
`Queue.recover_interrupted(stem)` is a near-copy of `resume_held(stem)`: merge
the brief into the top of the body, write to `pending/`, unlink the body,
**retain the sidecar as the permanent audit record**. Nothing is ever deleted.

Touch points in `queue.py`: `SUBDIRS`, `counts()`, `is_empty()`,
`_ARCHIVE_BLOCKERS` (interrupted work blocks archiving), `_next_index()`, plus
new `interrupted()` and `stranded_running()` queries.

`odin status` gains the state and its hint:

```
$ odin status go-rewrite

  pending      12
  done         79
  interrupted   1
    080-ui-port          2h ago  → odin recover
  held          0
  failed        0
```

### 5.1 The sidecar

`interrupted/080-ui-port.recovery.md`:

```markdown
# Interrupted — 080-ui-port

Attempt 2 pending. `odin recover` merges the brief below into the task body
and requeues it.

## Why
usage limit (confirmed) · resets 2026-07-25T15:00-04:00
> You've hit your session limit · resets 3pm (America/New_York)

## Attempts
| # | when | turns | wall | cost | left behind | wip commit | outcome |
|---|------|-------|------|------|-------------|-----------|---------|
| 1 | 2026-07-25T21:32Z | 85 | 10m35s | $8.07 | 8 files +1187/-43 | 7c21f0a | usage_limit |

## Resumption brief
<the text merged into the task body — see §7>

<details><summary>evidence</summary>

```json
{"run_id": "a9696b33…", "session_id": "31150f1e…", "exit_code": 1,
 "stop_reason": "stop_sequence", "error": "is_error with subtype=success",
 "branch": "go-rewrite", "head_before": "a3f91c2", "wip_commit": "7c21f0a",
 "confidence": "confirmed", "reason": "usage_limit",
 "resets_at": "2026-07-25T15:00:00-04:00",
 "porcelain": ["?? internal/ui/table.go", " M cmd/serve/main.go", "…"],
 "diffstat": "8 files changed, 1187 insertions(+), 43 deletions(-)"}
```
</details>
```

The **Attempts** table accumulates across cycles. It is both the audit trail
(satisfying "an interrupted task that was retried must be distinguishable later
from one that ran cleanly the first time") and the input to the no-progress
circuit breaker (§6.4).

---

## 6. Git and run semantics

### 6.1 The WIP commit

Performed by a new `git.commit_wip(project, *, stem, run_id, reason, exclude)`:

```
wip(odin): interrupted attempt at 080-ui-port

8 files changed, +1187/-43. Partial work from an interrupted attempt —
may not build or pass tests.

Odin-WIP: 080-ui-port
Odin-Run: a9696b33
Odin-Reason: usage_limit
```

**Timing: the commit happens at recovery, before the agent restarts.** Recovery
commits the uncommitted work, requeues the task, and the resuming agent then
builds on top of that commit and makes its own milestone commit when it
finishes. The WIP commit is an ordinary commit on the branch — at runtime it is
not treated specially in any way.

**Decision 5 is only about what happens to it afterwards: it is never
squashed.** The interruption stays visible in `git log` as its own commit,
rather than being folded into the finishing agent's milestone commit. Both end
states contain identical code:

```
# Decision 5 — the WIP commit remains
9f2ab1c Milestone 080: a UI port that cannot drop a column
7c21f0a wip(odin): interrupted attempt at 080-ui-port
a3f91c2 Milestone 070: alerts that arrive, and a tripwire for silence

# Rejected alternative — agent folds it in, WIP commit disappears
9f2ab1c Milestone 080: a UI port that cannot drop a column
a3f91c2 Milestone 070: alerts that arrive, and a tripwire for silence
```

Nothing about this is deferred and no cleanup is ever expected of the user. The
`Odin-WIP` trailer exists purely as a grep handle (`git log --grep=Odin-WIP`)
for anyone who *chooses* to squash later; leaving them in place forever is the
normal, supported outcome — and is what the motivating project already does with
its hand-written `b3c65a3`.

Rejecting the fold-in alternative also keeps a rule intact: Odin performs no
history surgery — no amend, no rebase, no soft reset — and never asks the agent
to either, least of all unsupervised at the moment of recovery.

Mechanics, each one deliberate:

- **Scope.** `git add -A` within the project, minus the queue dir when it lives
  inside the project — reusing the exact `ignore_within` semantics that
  `git.is_clean` already applies. (In the motivating project `queue/` is
  gitignored, so this is belt-and-braces, but it must not depend on that.)
- **Nothing to commit → no commit.** The 3-second / $0.00 case leaves a clean
  tree; recovery skips straight to requeue. Never an empty commit.
- **Hooks bypassed** (`--no-verify`), with a printed note. A pre-commit hook
  that rejects non-building code would block you at exactly the moment you are
  trying to get unblocked, and this commit is a checkpoint, not a contribution.
- **Secret guard.** If any file about to be committed matches a small deny-list
  (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `credentials.json`), Odin refuses to
  auto-commit, leaves the tree untouched, and tells you. "Never commit secrets"
  is a durable supply-chain rule; an automated `git add -A` is exactly the sort
  of thing that violates it by accident.
- **Failure is safe.** If the commit fails for any reason (signing, hooks that
  survive `--no-verify`, an index lock), Odin runs `git reset` to unstage,
  leaves the working tree exactly as it found it, prints git's error, and aborts
  recovery. Work is never lost, and the index is never left half-staged.
- **`--no-wip-commit`** leaves the tree dirty; the recovered run then needs
  `--allow-dirty` (a new, unscoped escape hatch on `odin run`) to start. This is
  the documented path for anyone who wants the old behavior, and it is the only
  place the dirty-tree question survives.

### 6.2 What the clean-tree check does

Nothing new. Because recovery commits, the tree is clean and
`_setup_branch`'s existing check passes untouched. No waiver, no snapshot
comparison, no staleness rule. This is the main structural simplification that
decision 1 bought.

### 6.3 The wait loop

When `resets_at` parses and the wait is accepted:

1. **Recover first, then sleep.** The WIP commit and requeue happen immediately,
   before any sleeping. If the laptop dies during a 4-hour wait, the work is
   already checkpointed and the queue is already runnable.
2. Sleep in short increments with a live countdown, adding a 2-minute buffer
   past the stated reset. `Ctrl-C` exits cleanly with exit 12 and the task
   already sitting in `pending/`.
3. `--max-wait` (default 360 minutes) caps it. A reset beyond the cap is
   reported but not offered.
4. On wake, the loop simply continues. No verification handshake — if the limit
   has not actually lifted, the next task is interrupted again and re-enters
   this same path. The whole loop is idempotent.
5. `term.py` paints the tab with a waiting state, so a backgrounded run is
   readable at a glance. Same posture as everything else there: best-effort,
   TTY-gated, metadata only.

### 6.4 Repeated interruption

A long task can legitimately span several usage windows, so attempt count is the
wrong circuit breaker:

```
attempt 1  85 turns  +1187/-43  → limit, sleep 2h13m
attempt 2  61 turns   +402/-88  → limit, sleep 4h02m
attempt 3  44 turns   +156/-12  → done ✓
```

The breaker is **progress**. An attempt counts as no-progress when it produced
`num_turns <= 1` **and** left no change in the tree (the WIP commit would be
empty). Two consecutive no-progress attempts stop the cycle:

```
✗ 080-ui-port · two attempts made no progress (0 turns, no file changes).
  This does not look like a usage limit. Check the agent binary and auth.
  Recover anyway with: odin recover 080-ui-port --force
```

The task stays in `interrupted/` with `blocked: true` in the sidecar; `--force`
overrides. This catches the real failure mode — an environmental problem
(missing binary, expired auth, broken MCP server) presenting as a repeating
interruption — without penalizing a genuinely long task.

### 6.5 Tasks stranded in `running/`

`_run_loop` calls `q.claim_running(task)` before `run_agent`, and
`q.next_pending()` reads only `pending/`. When Odin's own process dies — Ctrl-C,
closed laptop, OOM, dropped SSH — the file sits in `running/` forever and no
command will ever pick it up. Probably the most common interruption of all, and
currently unhandled.

Recovery adopts it: `reason="process_died"`, `confidence="probable"`, git
snapshot taken at recovery time rather than at failure time (there was no
failure path to run). The brief says so honestly — Odin does not know how far
the attempt got, only what it left behind. Everything downstream is shared.

---

## 7. The resumption brief

`recover` merges the brief into the task body, so the prompt the agent finally
sees is:

```
## Context from previous task      ← existing carry, from the last COMPLETED task
---
## You are resuming this task      ← the brief
---
<original task body>
```

Order matters: milestone context, then what your predecessor did toward *this*
task, then the task itself.

```markdown
## You are resuming this task

A previous attempt was interrupted after 85 turns (~10m) by a provider usage
limit. You are continuing it, not starting fresh.

Your predecessor's work is in commit 7c21f0a:
  new       internal/ui/table.go, internal/ui/table_test.go, … (5 files)
  modified  cmd/serve/main.go, internal/api/routes.go, internal/ui/model.go
  8 files changed, +1187/-43

The last completed milestone is a3f91c2
"feat(api): milestone 12 — price feed adapters".
Anything after that commit is your predecessor's unfinished work.

Its final output before it stopped:
  "…wired the table renderer; still need the column-width pass and the
   serve handler."

Before writing anything:
- Inventory what already exists against what this task requires.
- Finish partial work rather than rewriting it. If you do replace something,
  say why in your NEXT_CONTEXT.
- Prove each acceptance criterion with a test or command. A function existing
  is not evidence that it works.
- Compile errors in this area may be your predecessor's unfinished work, not an
  inherited defect — check before concluding the repository is broken.
```

Every element is derived, nothing invented:

| element | source |
|---|---|
| turns, duration, reason | `RunResult` + `Failure` |
| files and diffstat | `git.snapshot()` at interruption |
| WIP commit sha | `git.commit_wip()` |
| last milestone commit | `git log -1` before the WIP commit |
| final output | terminal `result` text, or the retained stream tail (§9.3) |
| standing instructions | fixed text |

**Re-merge is idempotent.** The brief is delimited by
`<!-- odin:resumption-brief -->` … `<!-- /odin:resumption-brief -->`. Recovering
a task that already carries a brief *replaces* it rather than stacking a second
one — otherwise a task interrupted three times accumulates three briefs and the
oldest, most misleading one appears first.

**On the standing instructions.** Item 6 of the brief is workflow-adjacent,
which brushes against "Odin contributes no rules." It stays on the right side of
the line: it is scoped to a single recovered task, it lives in the *task body*
rather than the injected system prompt, and it is about reconciling with prior
state rather than about how to build software. `--no-brief` falls back to a bare
one-line retry note.

**Verification output is opt-in.** `--verify-cmd` / `recovery.verify_command`
runs a command in the project and pastes its (truncated) output into the brief,
so the agent starts knowing exactly what is broken. Unset by default — Odin must
not guess build commands. When unset, the brief instead instructs the agent to
run the project's own verification first, per its instruction file.

---

## 8. Implementation surface

| File | Change |
|---|---|
| `backends/base.py` | `Failure`, `FailureKind`; `AgentBackend.classify_failure` defaulting to `DEFECT` |
| `backends/claude.py` | Three-tier `classify_failure`; limit-pattern + `resets_at` table; fix the `error: "success"` label; fall back to `accumulated_text` when there is no terminal event; return `text_delta` from assistant events |
| `runner.py` | Retain a bounded tail of streamed assistant text so a hard-killed run still has "last words" |
| `git.py` | `snapshot()`, `head_sha()`, `last_commit_subject()`; **`commit_wip()` — the first write operation**, with the secret guard and safe-failure semantics of §6.1 |
| `queue.py` | `interrupted` in `SUBDIRS`/`counts`/`is_empty`/`_ARCHIVE_BLOCKERS`/`_next_index`; `mark_interrupted`; `recover_interrupted` (modeled on `resume_held`); `interrupted()` and `stranded_running()` |
| `recovery.py` *(new)* | Sidecar read/write, attempt log, brief synthesis + idempotent re-merge, no-progress evaluation. Keeps `cli.py` from growing another 250 lines |
| `wait.py` *(new, small)* | Countdown sleep with buffer, cap, and clean `Ctrl-C`; injectable clock for tests |
| `cli.py` | `_cmd_recover`; parser wiring; `interrupted` branch in `_route`/`_run_loop`; startup detection + recovery offer; `_print_interrupted` replacing the misleading `failed/` hint; exit 12 |
| `metrics.py` | `"interrupted"` outcome; `RunAccumulator.interrupted`; `_STOP[12]`; **add `exit_code` to the task record** (§9.2); `wait_ms` on the run record; report renderers |
| `config.py` | `[recovery]` table: `wip_commit`, `auto_recover`, `wait_for_reset`, `max_wait_minutes`, `verify_command`. The hand-rolled TOML writer currently handles top-level scalars + `[platforms.<p>]` only and needs one more table shape |
| `term.py` | A waiting state for the tab title/color during a reset sleep |
| `guide.py` | Recovery section; `odin guide recovery` topic |
| `CLAUDE.md` | Queue layout; CLI surface; **the git non-goal amendment (§1.1)**; the brief's status as scoped, task-body-only guidance; exit-code table |

Zero new runtime dependencies. Timestamp parsing is `datetime.strptime` plus
`zoneinfo` against the small pattern table in §4 — no `dateutil`.

**Exit codes:** 10 = held, 11 = urgent halt, **12 = interrupted**.

---

## 9. Bugs to fix regardless

**9.1 `error: "success"`.** `backends/claude.py:174-175`. When `is_error` is true
and `subtype` is `"success"`, emit something meaningful instead of the literal
string `"success"`. Five of six failure records currently carry a nonsense error
string. One line.

**9.2 `exit_code` missing from the task metrics record.** `metrics.py:259-283`
logs `stop_reason` and `error` but not `exit_code` — one of the three
classification signals. The metrics log therefore cannot answer "was this an
interruption?" retrospectively, which had to be worked around while researching
this document. One line, and it makes the historical record analyzable.

**9.3 Streamed assistant text is dropped for Claude.**
`ClaudeBackend.handle_stream_event` returns `None` for `assistant` events, so
`runner.py`'s `text_parts` accumulator never fills, and `normalise_result` takes
`final_text` solely from the terminal `result` event. No terminal event → no
text at all, in precisely the hard-kill case where the agent's last output is
the only evidence. Return `{"text_delta": text}` and have `normalise_result`
prefer the terminal event but fall back to the accumulation. Improves every
failure diagnostic, not only recovery.

---

## 10. Test plan

Unit, no agent CLI required:

- `classify_failure` against all six historical failure signatures, asserting
  5 interrupted / 1 defect.
- An **unrecognized** limit message still classifies interrupted at tier 2 with
  `reason="unknown"` — the guarantee that pattern fragility cannot affect
  routing.
- `resets_at` parsing across all four message forms, including tz handling and
  the no-timestamp case.
- `commit_wip`: scope excludes the queue dir; clean tree makes no commit;
  trailers present; secret guard refuses and leaves the tree untouched; a forced
  commit failure leaves no staged index.
- `recover_interrupted` round trip: sidecar in, merged body in `pending/`,
  sidecar retained, body unlinked.
- Brief re-merge idempotency: recovering twice yields exactly one brief.
- No-progress breaker: progress → cycles; two no-progress attempts → blocked;
  `--force` overrides.
- Wait loop with an injected clock: buffer applied, cap respected, `Ctrl-C`
  leaves the task in `pending/` and exits 12.
- Stranded `running/` adoption.
- `interrupted` blocks archiving; `odin status` renders state and hint.
- Non-TTY halts at exit 12 and commits nothing.

End-to-end: extend `odin demo` with an eighth task that simulates an
interruption — a stub `claude` binary that writes a partial file, prints a limit
message, and exits 1 — exercising interrupt → recover → complete. The demo
already covers held→resume; this is the same shape and keeps the fixture the
honest end-to-end test it is meant to be.

---

## 11. What this looks like when it works

```
$ odin run --platform claude
━━ ⏵ task 81/92 · 080-ui-port ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   project ~/code/asset-api-go · branch go-rewrite
   …
⏸ 080-ui-port · interrupted · usage limit
   resets at 15:00 America/New_York (in 2h13m)
   8 files changed, +1187/-43

  → commit  7c21f0a  wip(odin): interrupted attempt at 080-ui-port
  → move    interrupted/080-ui-port.md → pending/
  → brief   resumption context merged into task body

Wait 2h13m and continue automatically? [y/N] y
odin: sleeping until 15:02 (Ctrl-C to stop)
   waiting · 2h12m remaining
```

…and four hours later the queue has drained, `git log --grep=Odin-WIP` finds the
two checkpoints to squash whenever convenient, and each recovered task's
`.recovery.md` records exactly what happened and how many attempts it took.

---

## 12. What the build changed

Six things the design got wrong or left open, found while implementing it.
Recorded here rather than edited into the sections above, so the reasoning
stays auditable.

**12.1 `--max-turns` had to be carved out as a defect.** The structural rule
("did the turn end on the agent's own terms?") classifies a `--max-turns` stop
as an interruption, and the first test run duly routed it to `interrupted/`.
That is wrong: `--max-turns` is the *user's own circuit breaker*. Recovering it
would commit the partial work and re-run straight back into the same cap, which
is exactly what the cap exists to prevent — and the no-progress breaker would
never fire, because each attempt does make progress. It now returns
`DEFECT / reason="max_turns"` and keeps its existing advice in `_print_failed`.
This is the one place where a signal that looks structurally like an
interruption is deliberately not treated as one.

**12.2 `--yes` must not imply consent to commit.** The first end-to-end run
committed and recovered in a non-TTY session, violating decision 9. Cause:
`_may_recover` accepted `args.yes`. But `-y` means "skip the platform/model
confirmation" — a script passing it is not thereby authorising Odin to write
git history. Only `--recover` does. Pinned by
`test_unattended_run_never_commits_on_your_behalf`.

**12.3 The TOML writer needed no changes.** §8 claimed `[recovery]` would need
a new table shape in the hand-rolled writer. Wrong — `_emit_table` already
recurses over arbitrary nested tables, and `odin config set
recovery.verify_command 'go build ./...'` worked unmodified.

**12.4 `odin recover <queue>` had to be disambiguated.** Both positionals are
optional with the stem first, so `odin recover go-rewrite` was read as a task
named "go-rewrite" in the default queue — which then reported the container
error. Naming the queue is at least as common as naming the task (there is
usually only one interrupted task), so a first positional that resolves to a
directory is now taken as the queue.

**12.5 Rename origins must not be staged.** The design had `commit_wip` staging
a rename's pre-rename path so the deletion half was recorded. Git only reports
`R` once the rename is *already staged*, at which point the old path matches
neither index nor worktree and `git add` fails outright with "pathspec did not
match any files". Origins are kept for the secret guard only. An unstaged
on-disk rename shows up as a separate delete + untracked pair and needs nothing
special.

**12.6 File counts must expand untracked directories.** Git collapses a wholly
untracked directory into a single `?? pkg/` porcelain entry, so counting
entries reported "1 file changed" for a task that created twelve files in a new
package. `_count_changes` now walks untracked directories.

Two smaller notes: `out: TextIO = sys.stdout` as a default argument binds the
stream that existed at *import* time, silently bypassing later redirection —
every new function resolves the sink at call time instead, matching
`runner.run_agent`. And the secret guard (§6.1) earned its place immediately:
it is exercised by a test in which the agent leaves a `.env` behind, and it
refuses before staging anything.
