# Design for Confirmation Responses and Resuming Generation

> 中文版: [中文版](confirmation-and-resume.zh-CN.md)

Confirmation responses are implemented in [confirmations.py](../src/folio/confirmations.py), the [conversation API](../src/folio/api/confirmations.py), the [Engine](../src/folio/orchestration/engine.py), and [ConfirmationPanel.vue](../frontend/src/components/ConfirmationPanel.vue). Failed or paused jobs are resumed separately through the job control API. Both mechanisms reuse the confirmed outline, business-task ownership, and existing checkpoints rather than regenerating the entire document body.

## Continuing Generation After Failure or Pause

When a body-generation job enters `failed` because of an execution error, such as a model API error, the page offers “Retry and continue” below the failure diagnostics. This remains available after refreshing or reopening the conversation; if the resume API fails, the diagnostics and resume action remain available. Paused jobs continue to use the resume button next to the input box.

Users can also say “Continue the previous task.” Intent recognition routes this through `resume_generation` to the resume branch, which first looks up the body-generation job bound to the current conversation and then calls `POST /jobs/{job_id}/resume`. Resuming retains the original job ID, confirmed outline, saved body text, research checkpoints, and successful-request cache. Failed or canceled unfinished steps are executed again without going through outline planning. If no body-generation job can be resumed, the system explicitly says so and must not regenerate the outline on its own.

Failure recovery does not reset the cumulative model-call count or silently increase the call limit. Drafts already delivered as `needs_confirmation` still use the confirmation-response flow described below; published documents and ordinary drafts are not resumed as failed jobs. For the same job that is already running, the system only resubscribes to progress and does not create duplicate background execution.

After a successful resume, the frontend subscribes to events for the same job and displays “Continuing body generation from saved progress.” Event replay starts at the most recent resume boundary, so historical failure or pause events do not override the current running state. The complete history remains in the backend audit trail.

## User Actions and States

After the complete body is delivered as `needs_confirmation`, the page lists the current issues and affected sections. Users may answer only some of the issues, or leave all answers blank and rerun unfinished checks after the model service recovers. Both actions require an explicit submission of the call allowance for the current round. Blank answers are not interpreted as approval of business rules. Unanswered business issues remain pending confirmation even if the model does not report them again in the current round. If one response resolves multiple related issues, users can enter it separately in each issue field. Technical and layout issues, along with cover and source-comment issues that have explicit checkers, are rebuilt through the corresponding checks rather than remaining permanently frozen because historical issues are retained.

Providing additional confirmation is optional; the existing document can be downloaded directly. The title bar can be expanded or collapsed. Clicking “Skip this round” leaves only a one-line notice and a “Provide responses” action, while preserving any entered responses. Skipping does not submit answers, start rechecking, increase the model-call allowance, or remove pending-confirmation markers from the document. Collapse and skip preferences are stored in the current browser by conversation, job, and confirmation `revision`, and persist across page refreshes and conversation switches. A new confirmation round expands automatically.

```text
needs_confirmation
  → User submits the current issue revision, optional responses, and this round's allowance
  → Archive the previous delivery, atomically save the resume record, queued / review
  → Read the existing body and apply this round's responses
  → Task and full-document review, necessary repairs, illustration checks, Word delivery
  → published / draft / needs_confirmation, or explicit failure
```

The normal execution entry point does not automatically restart jobs awaiting confirmation. Published documents and ordinary drafts cannot be modified directly through this API either. Resuming from the pending-confirmation state requires body checkpoints for every node in the complete outline. The existing pause/resume mechanism has a separate control API.

## APIs and Concurrency Protection

| API | Contract |
| --- | --- |
| `GET /jobs/{job_id}/confirmation?conversation_id=...` | Validates conversation ownership and returns the issue list, `revision`, cumulative call count, and default allowance for the current round |
| `POST /jobs/{job_id}/confirmation` | Accepts `conversation_id`, `request_id`, `revision`, `answers`, and `additional_calls` |
| `GET /jobs/{job_id}/confirmations/{request_id}/artifact` | Downloads the Word document archived before the corresponding resume round |

`answers` is a list of `{issue_id, answer, close_comment?}` objects, with `close_comment` defaulting to `false`. A submission may contain at most 40 entries, each answer may contain at most 600,000 characters, and the same issue cannot be submitted more than once or with a blank answer. Issue IDs use the v2 identity of `Finding`. The `revision` is calculated from the current issue snapshot, job input, section versions, and previous resume round, preventing stale pages from overwriting new issues.

On submission, the system first acquires the conversation and job locks, then uses a SQLite transaction to check the state, save the round, and queue the job. The client retains a stable `request_id` for each submission. Network retries must carry the same ID and exactly the same content. An identical request takes effect only once, without adding the allowance or writing responses again. Reusing the same ID with different content, submitting a job that does not belong to the current conversation, or submitting after the state or issue revision has changed all return a conflict.

The frontend saves unsubmitted responses and request IDs by conversation and job, and isolates asynchronous responses when switching conversations. After a successful submission, it resubscribes to job events. Old download messages point to the archived Word document, while the new delivery corresponds to the current run. The issue view, conversation restoration, and delivery events provide a list of historical drafts, allowing earlier versions to remain downloadable after a page refresh.

## Call Allowances and Recovery

The current round's allowance, `additional_calls`, defaults to 200 and permits values from 1 to 10000. The new job call limit is `calls already consumed + additional_calls`, rather than an unconditional addition to the old limit. The cumulative count is always retained, with a total limit of 100000. Any unused old allowance is not added separately, and API retries do not grant the allowance again. The successful-request cache continues to be reused. Attempt checkpoints for failed or unfinished requests are cleared in the new round, while previous failure details and counts remain in events and the resume audit trail.

The current round's repair allowance starts from each section's actual `revision` at submission time, so previous repair counts do not immediately deprive new responses of a revision opportunity. The current round remains subject to the original `max_repairs` limit. Knowledge-base and web-search counts continue to accumulate by business task and are not reset by confirmation responses. The soft allowance for illustration pregeneration uses a separate budget bucket named after the current total limit, but the overall call count still includes all previous attempts.

V2 file research establishes a new research-budget allocation for an explicit resume, with an identity that includes the current round's `request_id`. Ordinary pauses, resumes, and automatic repairs do not reset the allowance. Even when only the call allowance is increased and no new business responses are provided, previously unfinished research can be reentered without reusing an exhausted research session. Original tool records and old allowances are retained. New research-model calls still count toward the document-wide total, with allowance reserved for subsequent body generation and review.

## How Responses Are Incorporated into the Body

Each response, together with its original issue, source ID, and scoped sections, is added to an immutable requirements-revision record, updating `JobInput.requirements` and the complete requirements text. `ConfirmedReferences` provides business responses as project references with `human_confirmation=true`, preserving their sources and versions without modifying the uploaded originals or presenting user responses as external regulations or knowledge-base evidence.

Explicit business, cover, and layout responses are also saved as revision records in the current conversation, so they remain available for later outline replanning. Actions that only retry the service do not create new writing requirements. A new response to the same issue replaces the old response as a required project reference, while the original response remains in the round audit trail.

When processing the current round's responses, only the affected business tasks are reinterpreted, and only the content sections listed in the issues are directly revised. Sections are still written by their original business-task owners. Revision results must correspond to the assigned sections and increment each version exactly once. All body changes and the current round's completion markers are committed in the same transaction batch. A failure must not leave a round marked as applied when only part of its changes have been made. The remaining body text, outline, sources, and existing search allowances retain their original contracts. Subsequent full review can still discover other genuine issues and repair them under the original rules.

Cover responses are written to the designated cover fields. Responses to source comments retain their association with the original comments and do not close them by default. A comment is set to `resolved` only if the user explicitly selects “This response resolves this source comment” and the current round's body changes and `applied` state are committed atomically. Merely entering “Not sure” or submitting a nonempty answer does not automatically close a comment.

Page-layout and technical-interruption issues are not treated as new business facts. “Confirmation” does not bypass actual page checks or previously unfinished reviews. Technical retries with blank responses also rerun fact extraction and body verification previously marked `local-check-incomplete`, preserving the existing body rather than ending the resumed run with incomplete local checks carried forward. A response is not an instruction to delete an issue directly: applicable structural, source, and semantic validation must still be completed after revision.

Insufficient research allowance, unverified coverage, and incomplete calculation verification each have dedicated pending-confirmation items. Rechecking reviews the current coverage checklist and derived issues. Neither an empty gap list returned by the research model nor approval by an ordinary review can directly close these issues. An unverified calculation can be cleared only when the same artifact passes verification, or when an independent review confirms that the body does not use the claim and that the actual task requirements already have original supporting evidence. Checks are run again after body and source versions change.

The new review merges in business items awaiting confirmation from the most recent round's original issue snapshot that have not been explicitly answered. Successfully applied responses from all rounds are matched by exact issue identity; resolution is not inferred from a shared topic or section. Wrapper issues, such as interrupted detailed checks or exhausted repair allowances, are returned to the original detailed-check and repair chain if they can be mapped to an original candidate in the same snapshot. If no original issue can be verified, they conservatively remain pending confirmation. Another interruption before the new full review completes restores the previous round's issue list. Once complete new conclusions have been saved, old concerns are not reintroduced.

The current round retains the research-snapshot strategy for global facts, without adding fact subscriptions, automatic broadcasts, or real-time synchronization across tasks. Explicit dependencies and the final global review continue to provide consistency checks.

## Archiving and Validation Boundaries

`confirmation_rounds` stores requests, original issues, responses, section versions at submission, previous input/snapshots, and cumulative call counts. `jobs/{job_id}/confirmations/{request_id}/draft.docx` stores the previous delivery. The same directory also stores the current round's `input.json` and `requirements.json`. The initial `inputs/` snapshot remains unchanged to avoid presenting new requirements as initial input. Previous failed calls are stored separately in the event audit trail; original issues and the false-positive ledger are not erased.

Targeted regression tests cover stale-revision rejection, request idempotency, allowance boundaries, technical retries with blank responses, the evidentiary scope of business responses, per-section revisions, batch commits, old-draft archiving, and frontend recovery. Automated tests do not guarantee that a real model will correctly understand every user response. Pending-confirmation states and review results after resuming must be presented accurately.
