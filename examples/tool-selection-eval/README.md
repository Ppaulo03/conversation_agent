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
and then on `heldout.jsonl`. Measured on 2026-10-24 (`--min-score 0.05`): 98.0% / 100% hit rate with 0% fallback and
~75% of the tools removed (ADR-031 has the full table).

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

## Larger catalog (`large/`)

Eight tools is an easy catalog. `large/` is a 41-tool generic assistant (bookings, shop, cart and
orders, payments, customer data, business info, support, marketing) with its own messages:
`dataset.jsonl` (101: two per tool, 14 that need two tools, 5 that need none) and `heldout.jsonl`
(48, written separately and never used to choose a threshold). Several tools are deliberately close
(`order_status` / `order_track_shipping` / `payment_status`, `policy_get` / `faq_search`,
`support_open_ticket` / `handoff_to_human`).

`--sweep 0.05,0.1,0.15,0.25` evaluates several `min_score` values from ONE pass over the provider
(it calls it once per message) and prints hit rate, recall, fallback and the mean number of tools
shown, to read the accuracy x saving curve instead of guessing a threshold:

```
uv run python -m conversation_agent.app.tool_selection_eval \
  --catalog examples/tool-selection-eval/large/catalog.json \
  --dataset examples/tool-selection-eval/large/dataset.jsonl \
  --provider jev --token-env JEV_API_KEY --sweep 0.05,0.1,0.15,0.25
```
