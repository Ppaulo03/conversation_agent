# Tool selection evaluation

A labelled set of Portuguese messages and the tool catalog they are about, to measure a tool
selector (ADR-028) offline: does it keep the right tool, and how much does it save?

| File | What |
| --- | --- |
| `catalog.json` | 8 generic tools of a booking assistant (availability, get/create/cancel/reschedule booking, services, business info, save preferences) |
| `dataset.jsonl` | 51 synthetic messages, each with the tools the model needs (`[]` = none) |
| `heldout.jsonl` | 16 more messages written after the rules, never used to tune them |
| `rules.yaml` | keywords for the `rules` provider |

```
uv run python -m conversation_agent.app.tool_selection_eval \
  --catalog examples/tool-selection-eval/catalog.json \
  --dataset examples/tool-selection-eval/dataset.jsonl \
  --provider rules --rules examples/tool-selection-eval/rules.yaml
```

`--provider jev --token-env JEV_API_KEY` measures TypeSafe Jev (`--url` defaults to
`https://api.typesafe.ai`, `--model` to `jev-latest`; one request per message, so a run costs a few
cents; the key is read from the environment and never written anywhere). Run it on `dataset.jsonl`
and then on `heldout.jsonl`.

`--provider package.module:factory` runs any other `ToolSelector` (a classifier, embeddings) over the
same files; `--json report.json` saves per-example rows; `--min-hit-rate 0.9` fails below a floor.

Reading the numbers: **hit rate** is the share of messages whose expected tools were all kept (the
quality that matters: a miss costs a fallback call); **fallback rate** is the share that got the full
list because the provider found nothing (safe, but no saving); **tools/tokens removed** is the mean
saving. The trigger is bypassed, so this measures the ranking, not the production limits.

The same labelled messages also work as an evaluation set for an embeddings provider (and, with a
larger set, to pick `min_score`/`top_n` or as few-shot examples). 51 messages are enough to compare
providers, not to train one.

Add messages freely, but keep `heldout.jsonl` for blind checks.
