# Codex model for a deployment

The default model is `gpt-6.1-sol`. Set `META_RESEARCH_CODEX_MODEL=gpt-5.6-sol`
in an instance's startup environment to select `gpt-5.6-sol` for that instance.
The setting is read when the Python process starts. Unsupported values fail
at startup instead of silently selecting another model.

The selected model is shared by creation and research Companion, Idea,
DeepFetch, Plan, Bundle, Target, Reasoning, acquisition, and Writing. The
schema-only drafting adapter selects a separately verified catalog for the
same model. The diagnostic API and CLI accept the currently configured model;
they do not change production selection.

Changing the setting requires a restart of that instance. Allow running work
to settle first. Stored model identities, native sessions, and signed runtime
bindings remain unchanged; a configuration change does not migrate earlier
work or authorize resuming it under a different model. New work receives the
new model identity.

Model selection does not grant account access. Verify the exact model with
the instance's own authentication before enabling it. For example, a ChatGPT
login can succeed while the provider rejects a particular model. Do not
substitute a different model automatically after such a rejection.
