# #271 — task-level Writing API and readable Companion

This reopens only the 2026-11-05 scope of #271, on `origin/v1-test@57ee12d724ddd8af5c784a223f5e943796c58472`. #271 and #262 were open and unassigned at intake; no native blocker or overlapping open PR was found. The dirty original checkout was preserved in place. Historical delivery and its failures remain in [the original validation record](issue-271-validation.md).

## User path and credential boundary

The existing Writing form supports current Codex, a preconfigured `config_ref`, or `task_api` with exact endpoint, model, protocol, nonsecret instructions and `api_key_ref`. Main document/PPT choices, selected material versions, scientific basis and ordinary Quest workspace stay on the existing path.

A password can be saved separately through authenticated, CSRF-protected `POST /api/v1/companion/content-credentials`. Its response contains only a random reference. Key input uses transient component state and a dedicated request, bypassing page drafts and the generic pending-write store. The daemon stores credentials outside Companion workspaces, in a private directory/file (0700/0600 on POSIX), bound to the exact Quest, endpoint and model. This is the existing trusted local daemon security boundary, not a separate OS vault or a sandbox against a hostile process running as that daemon user. Optional `api_key_ref` repairs only the secret for that same binding; “保存密钥” does not start a new Writing turn.

An existing restricted environment is also supported: `META_RESEARCH_CONTENT_CREDENTIALS` points to a private JSON map of references to `api_key_env` and an exact `endpoints` list, with optional `models` and `quest_refs` restrictions. It does not expose arbitrary environment lookup. Existing `META_RESEARCH_CONTENT_MODELS` entries remain usable with their endpoint/model/api_key_env and default Chat Completions protocol. Secrets must never be placed in instructions or task text.

Writing model selection is stored with the first ordinary turn, atomically with native generation rotation and enqueue. The content tool resolves that selection for the current turn or a follow-up in the same generation. A conflicting selector or an external call when current Codex was selected is rejected. A new generation does not inherit the binding. Existing idempotency, cancellation and runtime fences remain authoritative; a response arriving after its fence was revoked is not delivered under the stale task. Cancellation cannot undo work already performed by an external server.

## Protocol and error contract

`assistant.content.generate` accepts explicit authorized text messages. For a bound Companion Writing task, only messages are needed. Research roots may supply a selected `config_ref` or task model object under their existing grant. The service appends neither Quest history nor directory contents. Optional instructions are content instructions, not executable code, arbitrary headers or a new protocol adapter.

| Protocol | Request/response |
| --- | --- |
| `openai_chat_completions` | Bearer authentication; model/messages; nonempty choices[0].message.content. Explicit truncation, tool completion or refusal is not successful prose. Legacy compatible endpoints without finish_reason remain supported. |
| `openai_responses` | Bearer authentication; model/input; completed output message/output_text blocks. Incomplete or refusal output is rejected. |
| `anthropic_messages` | x-api-key, anthropic-version 2023-06-01; model/messages, system text and max_tokens 8192; end_turn text blocks. Larger documents can be composed in bounded sections; tool calls and incomplete output are not supported as prose. |

These are nonstreaming text adapters. They do not promise arbitrary protocols, file uploads, model tool execution or multimodal content. Full URLs are required; redirects are rejected, HTTPS is required except localhost/loopback, and credentials never follow a redirected destination. Responses are bounded to 8 MiB and secrets echoed as output are rejected. Official protocol references: [Responses](https://platform.openai.com/docs/api-reference/responses/create), [Messages](https://platform.claude.com/docs/en/api/messages).

The existing #263 diagnostics distinguish authentication, permission, rate/quota, service failure, timeout/unknown outcome, connection failure, request rejection and protocol response failure. Missing/unauthorized credential references fail before network I/O. Provider error bodies are not returned. There is one request per call, no automatic retry and no model fallback. The original Companion receives reason/correction and keeps draft, selection and evidence; uncertain remote outcomes require checking the service and existing output before another call.

## Companion and delivery

The actual Companion resource and every-turn adapter instruction now ask for coherent explanations appropriate to a reader new to the field, then technical detail when asked. Uncertainty, material gaps and formal guidance remain explicit. Conversation brevity does not rewrite reviewed full text, broaden permissions or remove necessary terminology/data/citations from professional documents. Creation/manual branches receive the same reader-level reminder.

The shared Writing reference explains the real tool, supported protocols and recovery. The selected content model writes the requested prose; Companion remains responsible for checking, revising with that model, editable file production, independent review and actual rendering. External text alone is not file acceptance.

## Evidence

Local current-round evidence is preserved under `D:/meta_research/test/Funtation_model/.issue-271-reopen/`. Final run identities and results are recorded below after verification. Local protocol endpoint evidence is separate from actual native Agent evidence and from external supplier-model validation. #274/#262 final integrated research acceptance is not claimed by this bounded Writing work.
