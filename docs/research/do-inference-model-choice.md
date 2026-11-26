# ab-256: which DigitalOcean model categorises merchant names

Spike run 2026-11-26. Script: a throwaway copy of the prompt in
`app/services/ai_categories.py`, run against DigitalOcean Serverless Inference.

## Test set

- 190 merchants from the reviewed starter list (`beyondSavingdb/data/starter-merchants.csv`,
  high and medium confidence, categories among the defaults), by the name a statement shows
  (the first `match_pattern`), plus 10 made-up vague business names whose right answer is
  "unknown" (or "Other").
- Each scored against three category lists: the defaults; a renamed list (`Grocery`,
  `Phone & data`, `Travel & fuel`, `Eating out`, `Health`, `Bills`, `Side gigs`); and a short
  custom list (`Food`, `Bills`, `Getting around`, `Phone`, `Health`, `Fun`, `Kids`, `Home`,
  `Other`; `Pay`, `Other income`), where only merchants with a clear match are scored.
- Only public brand names and category names were sent. Temperature 0, the same prompt for
  every model.

## Results

| Model | Defaults | Renamed | Short custom | Duplicate new categories | Vague → unknown | Slowest batch | Output tokens per 200 names |
|---|---|---|---|---|---|---|---|
| `glm-5.3-flash` | 83% | 86% | 75% | 0 | 10/10 | 16–36 s | ~25k |
| `deepseek-v4.1-flash` | 83% | 85% | 78% | 0 | 10/10 | 25–47 s | ~45k |
| `openai-gpt-oss-120b` | 72% | 71% | 65% | 0 | 10/10 | 48–56 s | ~12k |
| `anthropic-claude-5.5-haiku` | – | – | – | – | – | – | HTTP 403 (needs account Tier 3) |
| `openai-gpt-6-luna` | – | – | – | – | – | – | HTTP 403 (needs account Tier 3) |

GLM and DeepSeek think before answering: with 50 names per call and an 8,000-token
limit about half their replies were cut off. Batches of 25 with 16,000 tokens fixed it
(the figures above). Invalid JSON after that: 0 (GLM), 1 batch (DeepSeek).

Cost: roughly $0.07 per 1,000 merchants for GLM or DeepSeek. Answers are cached for
everyone, so each merchant is asked about once.

Many "wrong" answers are arguable, or the starter label is: ZUKU → Subscriptions (we say
Utilities), NTSA → Taxes & government fees (Transport), StarTimes → Entertainment
(Subscriptions), KASNEB → Statutory contributions (School Fees). Some are plainly wrong
(SUPER METRO, a bus company → Groceries). GLM proposes new categories more readily
("Electronics" for Avechi, "Sports & Fitness" for Decathlon); they are only created if the
user accepts.

## Decision

- **Categories: `glm-5.3-flash`** - as accurate as DeepSeek, fastest, hosted by
  DigitalOcean. No fallback model: a failure only means no AI suggestion, and the merchant
  is asked again on a later upload. Switching is a setting (`DO_CATEGORY_MODEL`).
- **Statement layouts stay on `openai-gpt-oss-120b`** (ab-314, tested on real statements).
- **Option A** (one shared answer per merchant from the defaults, then mapped onto the
  user's categories) rather than one call per user: sending the user's list scored the
  same on renamed lists, so per-user calls would add cost without accuracy.
- Built in ab-336.
