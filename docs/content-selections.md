# Immutable content selections

A content selection names an accepted parent AssetVersion and an optional canonical relative path. It does not create another RM asset. `asset_binding` retains the original five fields and RM receipt; `path: null` selects the entire asset. `selected_hash` describes selected content and is distinct from the parent content hash.

New Target completions issue `meta-research/target-root-completion-manifest/v2`. Physical `entries` describe the captured assets honestly. Signed `selections` retain each logical declaration and its parent binding. Overlapping declarations capture their shortest declared ancestor once; each child is resolved from that frozen content. A child-only declaration does not expand to an undeclared parent. Different existing snapshot versions remain distinct.

Run implementation fingerprints use the logical path, explicit file/directory kind and selected content tree. Global manifest ordinals and archive encoding are excluded. The host issues the protocol version; an Agent's asserted revision cannot choose it. This implementation is for fresh runs and does not read or migrate old asset or completion protocols.

RM's selection reader describes, reads and exports files and native directories. Target directories use native directory capture, so no historical ZIP adapter is needed. A ZIP asset remains an ordinary file. Directory kind is explicit, including single-member and empty directories. Member paths are always relative to the selected root.

Environment and Dataset registrations supply selections, including whole assets with `path: null`; their retained parent `asset_bindings` are derived. Readers expose `selection_readers`, including the exact `selection_ref` to place in a Target's `direct_accepted_input_asset_refs`. Multiple selections of one parent remain separate inputs. Every new resource record stores the same selection representation.

The issued execution handle binds the selection reference, path and parent version while preserving the parent's RM/RG proof receipts. Delivery exports a parent cache once and links only the selected files into each read-only Target input. Verification checks the selected bytes and exact contract. Different parent versions do not share a cache. Retaining a small child retains its entire parent version; this is not independent physical garbage collection or a promise of zero copies across transport and temporary workspaces.

Malformed declarations become persisted typed rejection candidates before freezing, intake or graph registration. Issuer evidence, fence, permissions, I/O and damaged historical content remain host failures. Replays consume the persisted candidate and diagnostic, rather than reinterpret edited live files. Inline frozen content is published before the completion receipt, allowing recovery if the process stops before RM intake.

Each Target has independent completion-based retry backoff. A repeated RM-accepted/pending result is not progress; another Target's success or in-flight work does not reset its backoff. Cancellation remains responsive during backoff.

The implementation starts from `origin/v1-test` at `a9196982225126c79051b045b58565d8212d9238`. At the user's explicit direction, acceptance starts with fresh runtime data. Current-version crash recovery remains supported. Local source/fixture verification does not deploy the change, migrate old data or alter the existing 8771 research generations.
