# Quality evaluation gate

`cases.template.json` is a safe seed suite. Copy it to the ignored
`cases.private.json`, replace or extend the prompts with representative private
work, and include document IDs only when those documents are already indexed in
the private Qdrant store. Never commit private prompts, answers, or reports.

For promotion evidence, maintain at least 60 private cases divided evenly among
research, document/RAG, coding, tool planning, personal assistance, and voice.
Every material task family needs deterministic checks; RAG fixtures also need
fixed document IDs and source-label checks.

The production candidate is `quality` (Qwen 3.5 9B) at 8k context. Compare it
with the retained `agent` (Qwen 3 14B) profile using the exact same cases,
five deterministic seeds, context length, and two local judge perspectives.
Each private RAG case
must pin `rag_document_id` to one already-indexed fixture and include
checkable `checks` plus a short `reference_answer` for the judge.

Run the baseline:

```bash
docker compose exec gateway python scripts/quality_benchmark.py \
  --cases quality/cases.private.json \
  --transport gateway --model agent --mode adaptive --context-length 8192 --repeats 5 \
  --judge-model agent --judge-model quality \
  --output quality/reports/14b-8k.json
```

Run the quality candidate from the gateway container so Ollama remains private.
`--timeout 0` deliberately waits indefinitely. Test 4k, 8k, then 12k, keeping
only configurations that remain fully GPU-resident according to `ollama ps`.

```bash
docker compose exec gateway python scripts/quality_benchmark.py \
  --cases quality/cases.private.json \
  --transport gateway --model quality --mode adaptive --context-length 8192 --repeats 5 --timeout 0 \
  --judge-model agent --judge-model quality \
  --output quality/reports/quality-9b-8k.json
```

Create a blinded human pairwise packet, score every pair as `A`, `B`, or `tie`,
then gate the candidate. Keep the key private until the reviewer has finished.

```bash
python scripts/quality_pairwise_review.py \
  --baseline quality/reports/14b-8k.json \
  --candidate quality/reports/quality-9b-8k.json \
  --blind-output quality/reports/blind-pairs.json \
  --key-output quality/reports/blind-key.json \
  --review-template quality/reports/human-review.json
# Review blind-pairs.json; fill in human-review.json without opening blind-key.json.
python scripts/quality_gate.py \
  --baseline quality/reports/14b-8k.json \
  --candidate quality/reports/quality-9b-8k.json \
  --blind-key quality/reports/blind-key.json \
  --human-review quality/reports/human-review.json
```

The gate rejects any deterministic-check failure, criterion or task-family
regression, paired score regression, a non-positive stratified 95% lower
confidence bound, incomplete human review, or a human preference for the
baseline. It deliberately never modifies deployment configuration.

Public regression evidence is separate from this private blind-review flow.
See [`../evals/README.md`](../evals/README.md) for pinned IFEval, EvalPlus, and
LiveBench runs. A promotion must pass both the private gate above and
`scripts/eval_public_gate.py`; `scripts/eval_promote.py` runs them in sequence.
The repository research and adoption boundaries are documented in
[`../docs/agent-quality-research.md`](../docs/agent-quality-research.md).
