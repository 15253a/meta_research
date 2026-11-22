# Codex model selection

In a Quest’s **修改配置** dialog, select the Codex model for Idea, Plan,
Bundle, Target and Reasoning separately. By default, Idea and Reasoning use
`gpt-6.2-astra`, while Plan, Bundle and Target use `gpt-6.1-sol`; every stage
uses the model’s highest available reasoning preset. The
standard models use Ultra (effective reasoning effort `max`), while Luna uses
Max because that is its highest available preset. The selector lists the
visible models supplied by the current Codex CLI, including `gpt-6.2-sol`,
`gpt-6.2-astra`, `gpt-6.1-terra`, `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`,
`gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra` and `gpt-5.6-luna`.
Saving applies the selected model on that stage’s next new call. An
operation already sent to the provider retains its original model during
reconciliation and retry. The settings survive a service restart.

Other Codex roles and the schema-only drafting adapter use the deployment
model. Its default is `gpt-6.1-sol`. Setting `META_RESEARCH_CODEX_MODEL` in
the instance’s startup environment selects any of the visible models above.
The drafting adapter materializes a verified schema-only catalog for the
selected model. Diagnostic API and CLI requests accept the same model list.

Provider availability depends on the instance’s authentication. Verify
the selected model with the account on that instance. Provider rejection
is reported for the selected model.
