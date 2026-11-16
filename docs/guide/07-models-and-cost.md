# 7. Models and cost

## Decision: the code never depends on one model vendor

**The simple way.** Call one vendor's SDK directly from every place that needs a model.

**What goes wrong.** Prices change, a better model appears, a region does not offer your
model, or legal says data may not leave the country. Every call site must be rewritten.

**What we do.** Every model call goes through one small interface, the **provider**
(`agentic_reporting/providers/`). The rest of the code calls `provider.complete(...)` and gets
back text, the parsed JSON, token counts and cost. Providers today:

| Provider | What it talks to | Used for |
|---|---|---|
| `fake` | nothing; writes from the facts | tests and CI, $0, no network |
| `bedrock-converse` | any model on Amazon Bedrock, via the Converse API | **the default** |
| `bedrock` / `anthropic` | Claude, on Bedrock or Anthropic's API | optional tier, not used today |
| `ollama` | a model on your own machine | private mode, no network |

Switching model is a setting (`LLM_MODEL=...`), not a code change.

**In an enterprise.** This is an **LLM gateway**: one place for model access, keys, logging,
cost tracking and limits. Products like LiteLLM or a cloud API gateway play this role at scale.

## Decision: data stays in the country

The build runs in AWS Mumbai (`ap-south-1`). On Amazon Bedrock a model can be served three ways:

| Serving | Where the data goes | Our use |
|---|---|---|
| In-region | only Mumbai | Gemma, DeepSeek: **yes** |
| `in.` inference profile | Indian regions only | possible (Claude Haiku is offered this way) |
| `global.` inference profile | any AWS region worldwide | **no** for Indian enterprise data |

India's DPDP Act and many company policies care where personal and business data is
processed. Choosing in-region serving makes that an easy yes.

## Decision: choose the model by measurement, not by brand

**The simple way.** Use the most famous model.

**What goes wrong.** You pay 10 times more for an answer a cheaper model gets equally right.
Or the famous model is not offered in your region.

**What we do.** `eval/model_bench.py` runs the golden reports (chapter 8) through every
in-region candidate, three times each (models vary from run to run), and ranks them by
**cost per passing report**. A report passes only if every check is clean and every required
fact is mentioned. Result (15 Nov 2026, Bedrock list prices, Mumbai):

| Model | Reports passed | Right first time | Warnings | Cost per passing report |
|---|---|---|---|---|
| **Gemma 3 12B** | 6/6 | 24/24 sections | 10 | **$0.00058** |
| Nemotron Nano 3 30B | 6/6 | 21/24 | 36 | $0.00056 |
| Ministral 3 8B | 6/6 | 21/24 | 27 | $0.00072 |
| Ministral 3 14B | 6/6 | 21/24 | 9 | $0.00098 |
| Qwen3 32B | 6/6 | 23/24 | 43 | $0.00120 |
| DeepSeek V3.2 | 6/6 | 24/24 | 56 | $0.00424 |
| GLM 4.7 Flash | 5/6 | 22/24 | 43 | $0.00079 |
| Gemma 3 4B | 3/6 | 24/24 | 36 | $0.00037 |

Gemma 3 12B was chosen: tied for cheapest, every section right first time, and among the
fewest warnings for the reviewer. The chatbot benchmark agreed: Gemma got 48 of 48 SQL
results right at about $0.00024 per answer. DeepSeek V3.2 also got 48 of 48, with no
repairs, at about $0.0015, so it is the **escalation tier**: the model to switch to if Gemma
ever fails a benchmark.

**Price per token is not the whole cost.** Some models "think" before answering and bill those
hidden tokens. One candidate (Kimi) used about 300 output tokens for a sentence that DeepSeek
wrote in 50, and refused the `temperature` setting. Another (gpt-oss) spent its output limit
on reasoning and returned cut-off JSON. Measuring end to end catches this; reading a price
list does not.

## Why Claude is not used, and what "the Anthropic form" was

Claude models on Bedrock need a one-time use-case form per AWS account before the first call.
We did not submit it, because the cheaper in-region models passed every benchmark. Claude stays
supported in code as a third tier, to be used only if a benchmark shows both Gemma and DeepSeek
failing. This is the general rule: **escalate by evidence**, not by habit.

## Decision: structured output that is guaranteed, not hoped for

The model must return JSON in a fixed shape (chapter 4). The Converse provider uses a ladder:

1. **Native**: the shape is sent as a tool definition, and the model is forced to call that
   tool. The tool's input is the JSON. Every field is marked required (one model skipped an
   optional field on its first run, and every number came back uncited).
2. **Prompt**: if a model cannot do tools, the shape goes in the prompt and the reply is
   validated, with one repair round-trip.

## Decision: unknown price means no call (fail closed)

**What goes wrong otherwise.** A new model with no price row is costed at $0. A spending cap of
$1 can then never trigger.

**What we do.** Prices live in `providers/pricing.py`, taken from the AWS Price List API for
Mumbai. A model without a price row is refused **before** any call. Costs are list-price
estimates from the token counts the provider returns.

## Decision: a private mode with no network at all

For data that may not go to any cloud, `LLM_PROVIDER=ollama` runs the same pipeline against a
model on your own machine. A test patches every network connection to fail and proves that the
whole run (queries, checker, checkpoint, audit, publish) makes none. The model call itself is
stubbed in that test, so it proves the plumbing, not a local model's quality.

## What a release has to freeze

An AI feature has more moving parts than code: the model ID, the prompts, the specs, the
metric definitions, the data, the eval results. In an enterprise a **release manifest** records
all of them together (image digest, model ID, prompt hash, spec hash, eval scores), so a past
answer can be reproduced and a bad release rolled back. Here, the spec hash and model are in
every audit row and report footer; a full manifest is **designed, not built**.

Next: [8. Testing and evaluation](08-testing-and-evaluation.md)
