# Context Capacity Benchmark Report

- **Model**: mlx-community/Qwen3.6-35B-A3B-4bit
- **Model identity SHA**: e1da56d9576a45f650c47834b1a4d15f0ed2d18b34caac456727731ff2c19433
- **Mode**: copy
- **Seed**: 20260727
- **Max generation tokens**: 512
- **Tolerance tokens**: 32

## Summary

- **Total cases**: 3
- **Passed cases**: 3
- **Highest all-pass tier**: 65536

## Token and time totals

- **Rendered prompt tokens**: 196584
- **Prompt tokens**: 196581
- **Generation tokens**: 132
- **Generation seconds**: 408.17564308400324
- **Load seconds**: 4.813844958000118

## Tier-by-position pass rate

| Tier | Position | Passed | Total | Rate |
| --- | --- | --- | --- | --- |
| 65536 | start | 1 | 1 | 1.0 |
| 65536 | middle | 1 | 1 | 1.0 |
| 65536 | end | 1 | 1 | 1.0 |

## Failure counts

- No failures recorded.

## Case details

| Tier | Position | Trial | Rendered tokens | Outcome | Generation seconds |
| --- | --- | --- | --- | --- | --- |
| 65536 | start | 1 | 65528 | pass | 147.01868724999804 |
| 65536 | middle | 1 | 65528 | pass | 127.67859991700243 |
| 65536 | end | 1 | 65528 | pass | 133.47835591700277 |
