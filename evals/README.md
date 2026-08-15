# Public evaluation runner

This optional Compose profile evaluates both Local AI API surfaces separately:

- `model` maps upstream evaluators to `/v1/chat/completions`.
- `agent` maps the same evaluator requests to the five-stage graph endpoint and
  scores only its final answer.

The pinned source revisions are in `sources.lock.json`. The gateway image and
production dependencies never include these packages. The runner saves sources
and reports in named Docker volumes; they are intentionally not committed.

Build and download the pinned sources:

```bash
docker compose -f compose.yaml -f compose.evals.yaml --profile evals build eval-runner
docker compose -f compose.yaml -f compose.evals.yaml --profile evals run --rm eval-runner sync
```

Run a public suite for each surface. Runs are serial, use the configured 8k
context by default, and have no model-response deadline:

```bash
docker compose -f compose.yaml -f compose.evals.yaml --profile evals run --rm eval-runner run --suite ifeval --surface model
docker compose -f compose.yaml -f compose.evals.yaml --profile evals run --rm eval-runner run --suite ifeval --surface agent
docker compose -f compose.yaml -f compose.evals.yaml --profile evals run --rm eval-runner run --suite evalplus-humaneval --surface model
```

`evalplus-*` code is generated through the loopback evaluation proxy, then
executed only in a disposable child container with no network, repository bind
mount, Docker socket, or Linux capabilities. The parent runner needs the Docker
socket solely to create that child; do not grant the socket to any agent model.

Run the complete public round once per day in a non-overlapping loop:

```bash
docker compose -f compose.yaml -f compose.evals.yaml --profile eval-scheduler up -d eval-scheduler
```

The scheduler never changes the gateway’s model, prompt, or deployment
configuration. It writes a `latest-round.json` marker in the eval-results volume.

Promotion remains manual. First complete the private blind review, then require
both private and public gates:

```bash
python scripts/eval_promote.py \
  --private-baseline quality/reports/baseline.json \
  --private-candidate quality/reports/candidate.json \
  --blind-key quality/reports/blind-key.json \
  --human-review quality/reports/human-review.json \
  --public-baseline /path/to/public-baseline-round \
  --public-candidate /path/to/public-candidate-round
```

LiveBench is pinned to its public `2024-11-25` release and is limited to
reasoning, math, language, data-analysis, and instruction-following. Its coding
and agentic-coding lanes are deliberately excluded. RAGChecker is source-pinned
for future diagnostic work but is not a promotion gate until a local evaluator
is calibrated against blind human review.
