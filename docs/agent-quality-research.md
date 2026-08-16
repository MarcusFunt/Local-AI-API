# Agent-quality research and adoption boundary

The gateway keeps runtime orchestration small, local, and directly testable.
Third-party quality tooling belongs in the isolated evaluation environment, not
the serving image. Every addition must be revision-pinned, license-reviewed,
and pass the private and public promotion gates before it changes a profile or
deployment default.

## Candidates

- [DSPy](https://github.com/stanfordnlp/dspy) is the preferred offline prompt
  optimizer. Use it only with the private evaluation suite to generate
  candidate instructions or demonstrations; save its output as a reviewed,
  versioned artifact rather than allowing runtime prompt mutation.
- [RAGChecker](https://github.com/amazon-science/RAGChecker) is already pinned
  in `evals/sources.lock.json`. Calibrate it against the fixed private RAG
  fixtures and blinded human review before enabling it as a promotion gate.
- [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) is the candidate
  for reproducible Agent Zero tool-trajectory evaluation in an isolated
  sandbox. Do not grant an evaluator a production workspace, credentials, or
  Docker socket beyond the existing least-privilege EvalPlus child container.
- [Agent Zero](https://github.com/agent0ai/agent-zero) is the upstream base for
  the existing overlay. Track it through the candidate-image workflow; retain
  the strict JSON tool-call contract unless a replacement demonstrates a
  measurable improvement.
- [lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness)
  is useful only when it adds local-model coverage beyond the pinned IFEval,
  EvalPlus, and LiveBench runner already present here.

## Deliberate non-adoptions

[LangGraph](https://github.com/langchain-ai/langgraph),
[Promptfoo](https://github.com/promptfoo/promptfoo), and
[DeepEval](https://github.com/confident-ai/deepeval) are not runtime
dependencies. Their current orchestration or evaluation capabilities overlap
with this project’s explicit graph and local promotion flow; revisit them only
when a measured gap cannot be filled by the isolated evaluation tools above.
