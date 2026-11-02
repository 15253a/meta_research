# Codex model selection

In a Quest’s **修改配置** dialog, select the Codex model for Idea, Plan,
Bundle, Target and Reasoning separately. Each stage defaults to
`gpt-6.1-sol` and uses Ultra (effective reasoning effort `max`).
Saving applies the selected model on that stage’s next new call. An
operation already sent to the provider retains its original model during
reconciliation and retry. The settings survive a service restart.

Other Codex roles and the schema-only drafting adapter use the deployment
model. Its default is `gpt-6.1-sol`. Setting
`META_RESEARCH_CODEX_MODEL=gpt-5.6-sol` in the instance’s startup
environment selects the other supported deployment model. The drafting
adapter uses a verified catalog for that model. Diagnostic API and CLI
requests use the deployment model.

Provider availability depends on the instance’s authentication. Verify
the selected model with the account on that instance. Provider rejection
is reported for the selected model.
