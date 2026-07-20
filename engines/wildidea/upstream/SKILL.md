---
name: wildidea
description: Generate concrete cross-domain innovation ideas by forcing far-domain mechanisms into product, strategy, research, algorithm, design, or invention questions. Use when the user asks for WildIdea, idea generation, innovation directions, escaping conventional thinking, "他山之石", card-style inspiration, or mapping distant domain mechanisms back to a current problem. This is an agent skill workflow, not a user-facing CLI.
---

# WildIdea Skill v1.3

Run WildIdea as an agent workflow. Draw a distant source first, freeze its concrete phenomenon and transferable method, then map that method back to the user's problem as an implementable proposal.

Do not expose the internal Python helpers as the product. The user should experience an idea-card workflow.

## Core Order

Always preserve this order:

1. Capture the problem and the user's common solutions to avoid.
2. Detect `algorithm`, `research`, `product`, or `strategy`.
3. Restate the problem as a `problem_card` before drawing any slot: five plain-string keys `actor`, `constraint`, `bottleneck_relation`, `desired_change`, `trade_off`. This step only structures the problem itself; it must not contain a solution.
4. Draw source slots before writing any target-domain solution.
5. Freeze the concrete source phenomenon as `他山之石`.
6. Abstract a target-domain-free method from that phenomenon.
7. Map the method into a concrete proposal in the user's domain, checked against the `problem_card`.
8. Run basic readability and actionability checks.
9. Send the candidate to an independent judge, who also checks the mapping against the `problem_card`.
10. Reroll that slot when it misses the quality gate or judge threshold, following the three-stage repair sequence in Web-Parity Contract — never a blind resample — subject to the retry limit.
11. After all 9 slots finish, run the batch-level diversity check (see Batch Diversity Check) before finalizing delivery; redraw the most redundant card from a new source domain when fewer than roughly 6 essentially distinct solution directions remain.
12. Run live novelty search, render the final HTML, validate it, and return its path.
13. After all 9 slots finish, audit `problem_card` coverage across the 9 cards' declared target keys and disclose any key none of them targeted.

Never start from a conventional answer and decorate it with a distant-domain name afterward.

## Problem Card

Before drawing slots, restate the user's problem as a `problem_card`: `actor` (who is stuck), `constraint` (what currently limits them), `bottleneck_relation` (the relation/link that is jammed), `desired_change` (the change they want), `trade_off` (the trade-off they must choose between). All five are plain strings describing the problem, not a solution.

- `problem_card` only structures the problem; it must never contain a mechanism name, target-domain conventional fix, or any proposal.
- Every card's mapping step and the independent judge must check the candidate against `problem_card`, not just against the raw problem text.
- Each card's `desc` must open by declaring which single key it primarily targets (see Card Contract).
- After the 9 slots finish, tally which of the five keys were actually targeted by at least one card. Disclose any key that no card touched — do not silently omit it.

This does not conflict with mechanism-transfer.md's rule that source-domain freezing must not look at the target domain: `problem_card` structures the problem side only, before any source mechanism is chosen, and produces no solution.

## Web-Parity Contract

Match the current Web v1.4 generation logic unless the user explicitly requests a different count or mode:

- Draw **9 cards by default**. Treat 9 as the normal maximum for one run.
- Generate and judge each slot independently. A completed card may be reported immediately; do not wait for every other slot before acknowledging it.
- Give each slot at most **3 total attempts**: the initial attempt plus at most **2 rerolls**. Use the three attempts as a fixed repair sequence, never a blind resample:
  - **Attempt 1 -> attempt 2 (repair)**: if attempt 1 fails the quality gate or the judge threshold, attempt 2 must carry forward the specific rule name(s) or judge dimension(s)/explanation that failed, and rewrite only the field(s) those failures point to. Never resubmit without carrying that concrete failure reason into the prompt.
  - **Attempt 2 -> attempt 3 (reangle)**: if the same failure cause repeats on attempt 2 (same quality rule id, or the same judge dimension still short), attempt 3 must change structural angle on the same source anchor — do not switch anchors, and do not just reword the same mapping again.
- The quality gate has three tiers, selected by `risk_profile`; default to `pragmatic` unless the user asks to go wilder/explore, or is feeding candidates into an automated research/verification pipeline.
  - `pragmatic` (default): `Structural Depth` reaches the judge-model-calibrated threshold, `Novelty >= 7`, and `Applicability >= 8`.
  - `explore`: `Structural Depth` reaches the same threshold, `Novelty >= 7`, `Applicability >= 7`, and at least one of `Domain Distance >= 7` or `Unexpectedness >= 8`.
  - `research`: `Structural Depth` reaches the same threshold, `Novelty >= 8`, `Applicability >= 6`, and `Domain Distance >= 7`. This tier is tuned for feeding an auto-research pipeline (idea -> implementation -> benchmark verification): it deliberately relaxes immediate feasibility and raises the novelty/domain-distance floor instead, so a wild-but-falsifiable candidate is not screened out just for lacking day-one Applicability.
  - Switch to `explore` or `research` when the user says things like "更野"/"探索模式"/wilder/explore mode, or wants research-grade exploration over immediate practicality (e.g. an auto-research integration), and disclose which tier was actually used when delivering results.
- If all 3 attempts fail the quality gate and never reach the judge, keep the draft with the fewest rule violations among the 3 attempts as a `未达标保底` delivery instead of a total slot failure.
- If no attempt passes the judge after 3 tries but at least one valid, scored, search-confirmable candidate remains, keep the eligible attempt with the highest mean of `Structural Depth`, `Domain Distance`, `Novelty`, and `Applicability`. Mark it `未达标保底`; do not describe it as passed.
- A malformed candidate, unsupported source, or confirmed direct duplicate is not eligible as a fallback. If every attempt is unusable (including the quality-gate floor above), retain a failed slot instead of inventing a result.
- Do not keep drawing indefinitely until 9 cards pass. Preserve the 9 original slot outcomes so the user can see where quality gates failed.
- The Web product refunds a failed/fallback card. The standalone Skill has no credit ledger, so preserve the quality label but do not claim that money or credits were refunded.

## Slot Pool

Use `scripts/pick_domain_slots.py` and read only its sampled JSON. Do not load the full pool unless editing it.

- `D1`: algorithm and technical mechanisms
- `D2`: academic, scientific, and engineering mechanisms
- `D3`: humanities, art, architecture, and narrative mechanisms
- `D4`: product, interaction, and business mechanisms
- `D5`: social, policy, governance, and institutional mechanisms
- `MAO` / displayed `D6`: 毛选 methods
- `RANDOM_WORD` / displayed `D7`: random-word disruption

The default per-type quota (see the Detailed Spec's Type And Slots table) no longer allocates a `RANDOM_WORD` slot to `algorithm` or `research` questions — a random word is low-signal noise rather than a useful disruption source for hard-science problems. `product` and `strategy` still each draw one `RANDOM_WORD` slot.

Supported pool modes mirror the website:

- `default`: use the problem-type quota from `references/domains.json`
- `social_policy`: 9 x `D5`
- `algorithm`: 9 x `D1`
- `product`: 9 x `D4`

Use `--pool-mode <mode>` when the user selects a preset. Keep D numbers tied to the top-level pool; use the adjacent domain label for the specific discipline.

By default, exclude anchors already marked as a completed cross-domain mapping (a finished, packaged case study) — reusing one risks a double-hop analogy that just borrows someone else's existing packaging. Only include them when the user explicitly asks to go wilder, via `--include-completed-analogies`, and disclose that they were included.

## Card Contract

Each candidate must contain:

- a short Chinese title;
- the slot number and specific source domain;
- `source_phenomenon`: a concrete source-world event, rule, operation, or constraint, written mainly in Chinese;
- `proto`: the abstract transferable method, containing no target-domain terms;
- `claimed_method`: the real-world existing method, algorithm, or technique that the source mechanism corresponds to (for example 卡尔曼滤波 or 岛屿生物地理学模型). When no genuine real-world counterpart exists, write `抽象概括（无真实具名对应）` instead of inventing one — never fabricate a method name that does not exist;
- `desc`: 2-4 operational sentences, opening with which `problem_card` key this card primarily targets, then naming inputs/materials, action order, trigger, resulting change, and a visible or measurable output;
- `advantage`: a plain-language sentence beginning with `这种方案的优势在于，`, preferably within 50 Chinese characters;
- `fail`: a concrete hidden premise or failure condition, retained as internal/detail metadata even when the compact card hides it.

For technical English terms, show the Chinese name first and place only the necessary acronym in Chinese parentheses, such as `随机采样一致性（RANSAC）`.

Reject a candidate when removing the source mechanism leaves only industry common sense, a renamed conventional solution, or a vague "use X to improve Y" sentence.

## Batch Diversity Check

After all 9 slots reach a final per-slot outcome (pass, `未达标保底`, or failed), run one more check across the whole batch before delivery:

- Judge whether the 9 cards represent roughly **6 or more solution directions that are essentially different** — different core mechanisms or angles of attack, not just different wording or a different source domain dressing the same fix.
- Count cards as duplicates of the same direction whenever their core mechanism or approach is essentially the same, even if their titles, `source_phenomenon`, or domains look different on the surface.
- If distinct directions fall below roughly 6, treat it as set-level mode collapse (generation converging on one class of fix), not a per-card quality defect: redraw the most redundant card from a new source domain instead of delivering a homogeneous batch.
- Any redrawn replacement card must still pass the full quality gate and independent judge — do not wave it through unchecked.
- Disclose the counted number of essentially distinct directions when delivering results.

## Random Word Rule

Keep the sampled word as the source. Ground it with a real search result before extracting an operation or rule. Do not silently replace it with an unrelated natural or scientific mechanism. If grounding is weak, reroll or fail the slot.

`RANDOM_WORD` slots do not use the 6-dimension structural-mapping score. Judge them with the dedicated `judge_random_word.txt` template instead, on 4 dimensions (0-10 each): `chain_clarity`, `actionability`, `novelty`, `unexpectedness`. Pass line: `chain_clarity >= 6` and `actionability >= 6` and `novelty >= 7` (an initial, uncalibrated bar).

## Target-Field Novelty Check (independent per-run toggle)

A candidate that clears the independent judge still gets one more check whenever the novelty-check toggle is active for this run (see the activation rule below — no longer tied to `risk_profile == "research"`): whether its `claimed_method` is already a known or standard method *for the target problem/field*, not just whether the source-domain analogy reads as unusual. The six-dimension score only judges the analogy — it can score a pairing like Mixture-of-Experts routing for cross-subject EEG transfer as freshly novel because the source/target labels look far apart, even though that pairing is already an established approach in that literature.

- Default implementation branches by problem type: `algorithm`/`research` questions search arXiv (academic search, e.g. via `https://export.arxiv.org/api/query`) rather than the generic-web sogou fallback — sogou's signal is too weak for judging whether *this specific named-method-for-this-specific-problem combination* is already published work. `product`/`strategy` questions keep using the generic web search for this check.
- The evidence question stays narrow either way: only "is `claimed_method` applied to this specific problem already existing published/public work" counts as known. Many hits about the method itself (famous in its own origin field, or plainly related papers that don't target this problem) do not make it known — the pairing with *this* target problem/field is what must already exist.
- The judging model is asked once whether the results show the claimed method (applied to this problem) is already standard/known; any search or parsing failure degrades gracefully to "not known" rather than penalizing the candidate — a tool failure must never manufacture a false known-method verdict.
- An auto-research integration may swap in its own literature-search backend (its own paper corpus, Semantic Scholar, etc.) in place of the default arXiv/web search, through the same pluggable backend parameter.
- **Closed loop instead of a passive label**: a candidate flagged `is_known` in this profile is no longer just labeled "领域内已知" (known in the target field) and left as-is. The slot rerolls from a **new source anchor** (never a reworded restatement of the same anchor) and repeats the full pipeline — quality gate, independent judge, then this novelty check again — up to **`max_novelty_rerolls`** additional times. This turns the online novelty check from a passive filter into an active novelty driver, so that both what gets delivered and what feeds an auto-research pipeline stays as free as possible of collisions with already-published work.
- If `max_novelty_rerolls` is exhausted and the candidate is still flagged `is_known`, keep the last version, label it "领域内已知", and disclose that the anchor-reroll budget was exhausted — never reroll indefinitely.
- **Activation, decoupled from `risk_profile`**: whether this check runs is decided solely by `Config.research_novelty_check`, a tri-state `Optional[bool]` (default `None`). `None` auto-selects by tier (on for `research`, off for `pragmatic`/`explore`); `True` forces it on regardless of tier; `False` forces it off regardless of tier. Both call sites that used to test `risk_profile == "research" and research_novelty_check` — the per-candidate gate and the closed-loop reroll gate — now go through one shared helper, `_novelty_enabled(config) = config.research_novelty_check if config.research_novelty_check is not None else (config.risk_profile == "research")`, so a candidate no longer has to be on the `research` tier for this check to run. The CLI exposes `--novelty-check` / `--no-novelty-check` (omitted = `None`, auto by `--profile`); the web app adds a "是否查重" (run novelty check) toggle (`CreateRunRequest.novelty_check`, default `False` — off unless the user opts in) that is stored in `config_snapshot` and passed to `Config.research_novelty_check` as an explicit `True`/`False` (the web layer always passes an explicit bool, never `None`, so a web-originated run never falls into the by-tier auto branch).
- Exists to avoid handing an auto-research pipeline a repackaged known method as if it were new, burning implementation and benchmark compute on it for nothing.

## Standard-Mode Requirements

The standalone Skill preserves the web quality gate and adds stricter execution evidence:

- **Live search**: use the current agent's online search capability for every final candidate. Search products, papers, code, patents, datasets, or nearest neighbors as appropriate. Local helpers are fallback utilities, not substitutes for real online search.
- **Independent judge isolation**: the website already uses a separate judge call; the Skill must additionally keep that judge in a fresh sub-agent/evaluator context. The generator must not self-score. Retain all 6 dimensions: Structural Depth, Domain Distance, Applicability, Novelty, Unexpectedness, and Non-Obviousness — except `RANDOM_WORD` slots, which use the separate 4-dimension judge described in Random Word Rule.
- **Structural Depth checks systematicity**: when scoring Structural Depth, the judge must explicitly verify whether the source→target correspondence points are strung together by one shared causal or functional relation (systematicity), rather than being isolated coincidences of surface-level properties. Score isolated surface coincidences low even when several correspondence points are listed.
- **Validated HTML**: create `outputs/<topic>.html` plus the search sidecar and validate the artifact before replying.

If one of these capabilities is unavailable, say exactly which standard step is blocked or switch only with the user's explicit permission to a non-standard quick/text draft. Never silently downgrade.

## Deepen (Optional Stage Two)

After the user picks one delivered card for a closer look, produce a deepen artifact for that single card instead of drawing a new slot:

- A minimal falsifiable experiment: a control/comparison object plus a concrete criterion for what would count as failure. Reuse the `最小可证伪实验` field spec already defined in `references/mechanism-transfer.md` — that spec is domain-general, not exclusive to algorithm/research cards; apply it to product, strategy, and design cards too.
- Verification-or-avoidance steps for the card's `fail` field: how to check whether the hidden failure premise actually holds for the user's situation, and what to do if it does (mitigate, substitute, or narrow scope).
- Keep this stage optional and per-card. Do not run it for all 9 cards unless the user explicitly asks for that.

## Detailed Spec

Read the following before a standard run:

- `references/wildidea-skill.md` when present in the installed package; otherwise `docs/wildidea-skill.md` in the source checkout
- `references/search-integration.md`
- `references/mechanism-transfer.md` for algorithm or research questions, and for its `最小可证伪实验` field spec reused by Deepen on any problem type
- `references/poster-guide.md`
