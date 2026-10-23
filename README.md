# Switchyard

An LLM gateway that keeps answers flowing when providers fail. When a provider dies halfway through a streamed answer, Switchyard finishes the answer on another provider, and measures how well each way of doing that works.

**Status:** week 1 of 5. Before building the gateway, a small test checks how seamless the joins look when one model finishes another model's interrupted answer. The full plan is in [SPEC.md](SPEC.md).

## The question

API7's AISIX gateway already continues broken streams, and LiteLLM did until it removed the feature from chat completions. What nobody publishes is how good the joins are: how often a reader can tell the answer was interrupted, by strategy and provider pair, and what each failover costs. That table is the point of this project.

## Week 1 seam test

The test asks Claude, GPT and DeepSeek for full answers to 30 prompts, cuts each answer at a random point, and has another model finish it five ways:

| Strategy | What happens |
| --- | --- |
| R, restart | The fallback answers again from the top, so the reader sees the start twice |
| I, instructed | The fallback is told the answer was cut off and asked to continue from the exact point |
| I+S | The same continuation as I, cleaned up by [seam repair](src/switchyard/streaming/seam.py) |
| A, anchored | The partial answer is quoted in the request, the fallback starts by copying its last few words, and seam repair cuts there |
| N, native | DeepSeek's beta prefix completion continues the partial answer directly |

Seam repair only post-processes text, so its rules can be improved against a finished run without new API calls:

```sh
uv run python -m eval.seam.rescore eval/seam/results/<run>
```

Run it offline with a fake provider:

```sh
uv sync
uv run python -m eval.seam.spike --dry-run
```

Run it for real (about 360 API calls; the script prints a worst-case cost and stops if it is over the budget in `eval/seam/config.yaml`):

```sh
cp .env.example .env   # then add your three keys
uv run python -m eval.seam.spike
```

Results land in `eval/seam/results/<timestamp>/`: `report.md` shows every join side by side, plus a summary table, raw JSONL and a labels sheet.

## License

Apache 2.0
