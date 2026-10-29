# Real-API smoke test, 2026-10-29 07:07 UTC

19 passed, 1 passed with warnings, 1 failed, 0 for information. Commit 960dd2b. Cost $0.0016.

The gateway ran locally with `config/switchyard.yaml`, calling the real APIs. Each model was called by name, so a failure shows instead of failing over. `breaker probe` is the one-token request an open breaker sends to recover; `prompt cache` calls the adapter directly with the same long prompt twice.

| Model | Check | Result | Time to first token | Detail |
| --- | --- | --- | --- | --- |
| anthropic/claude-haiku-5-5 | stream text | pass | 843 ms | 28 in, 15 out; '2, 3, 5, 7, 11' |
| anthropic/claude-haiku-5-5 | unary text | pass |  | 28 in, 15 out; '2, 3, 5, 7, 11' |
| anthropic/claude-haiku-5-5 | stream tool call | pass | 946 ms | 498 in, 40 out; 'get_weather({"city": "Astana"})' |
| anthropic/claude-haiku-5-5 | json | pass |  | 214 in, 12 out; '{"answer": "Astana"}' |
| anthropic/claude-haiku-5-5 | temperature | warn |  | 14 in, 5 out; 'Hello'; temperature dropped by config (sampling_params: false) |
| openai/gpt-6-luna | stream text | pass | 2350 ms | 20 in, 16 out; '2, 3, 5, 7, 11' |
| openai/gpt-6-luna | unary text | pass |  | 20 in, 16 out; '2, 3, 5, 7, 11' |
| openai/gpt-6-luna | stream tool call | pass | 939 ms | 135 in, 18 out; 'get_weather({"city":"Astana"})' |
| openai/gpt-6-luna | json | pass |  | 43 in, 12 out; '{"answer":"Astana"}' |
| openai/gpt-6-luna | temperature | pass |  | 12 in, 4 out; 'Hello' |
| deepseek/deepseek-flash | stream text | pass | 1230 ms | 18 in, 13 out; '2, 3, 5, 7, 11' |
| deepseek/deepseek-flash | unary text | pass |  | 18 in, 13 out; '2, 3, 5, 7, 11' |
| deepseek/deepseek-flash | stream tool call | pass | 738 ms | 281 in, 34 out; 'get_weather({"city": "Astana"})' |
| deepseek/deepseek-flash | json | pass |  | 45 in, 7 out; '{"answer": "Astana"}' |
| deepseek/deepseek-flash | temperature | pass |  | 10 in, 2 out; 'Hello!' |
| chat-fast | route group | pass |  | 14 in, 5 out; 'Hello' |
| anthropic/claude-haiku-5-5 | breaker probe | pass |  | one-token probe answered in 706 ms |
| openai/gpt-6-luna | breaker probe | fail |  | probe outcome client_error: an open breaker on this model could never close |
| openai/gpt-6-luna | prompt cache | pass |  | first call 2639 in, 2636 written to cache; second call 2636 read from cache |
| deepseek/deepseek-flash | breaker probe | pass |  | one-token probe answered in 1166 ms |
| deepseek/deepseek-flash | prompt cache | pass |  | first call 2727 in, 0 written to cache; second call 2560 read from cache |
