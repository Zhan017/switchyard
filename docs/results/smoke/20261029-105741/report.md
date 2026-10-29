# Real-API smoke test, 2026-10-29 10:57 UTC

7 passed, 0 passed with warnings, 0 failed, 0 for information. Commit 90148bb. Cost $0.0004.

The gateway ran locally with `config/switchyard.yaml`, calling the real APIs. Each model was called by name, so a failure shows instead of failing over. `breaker probe` is the tiny request an open breaker sends to recover; `prompt cache` calls the adapter directly with the same long prompt twice.

| Model | Check | Result | Time to first token | Detail |
| --- | --- | --- | --- | --- |
| openai/gpt-6-luna | stream text | pass | 993 ms | 20 in, 16 out; '2, 3, 5, 7, 11' |
| openai/gpt-6-luna | unary text | pass |  | 20 in, 16 out; '2, 3, 5, 7, 11' |
| openai/gpt-6-luna | stream tool call | pass | 1177 ms | 135 in, 18 out; 'get_weather({"city":"Astana"})' |
| openai/gpt-6-luna | json | pass |  | 43 in, 12 out; '{"answer":"Astana"}' |
| openai/gpt-6-luna | temperature | pass |  | 12 in, 4 out; 'Hello' |
| openai/gpt-6-luna | breaker probe | pass |  | probe answered in 1168 ms |
| openai/gpt-6-luna | prompt cache | pass |  | first call 2638 in, 2635 written to cache; second call 2635 read from cache |
