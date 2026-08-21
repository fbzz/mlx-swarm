# Context Capacity Benchmark Report

- **Model**: mlx-community/Qwen3.6-35B-A3B-4bit
- **Model identity SHA**: e1da56d9576a45f650c47834b1a4d15f0ed2d18b34caac456727731ff2c19433
- **Mode**: retrieve
- **Seed**: 20260727
- **Max generation tokens**: 512
- **Tolerance tokens**: 32

## Summary

- **Total cases**: 3
- **Passed cases**: 3
- **Highest all-pass tier**: 65536

## Token and time totals

- **Rendered prompt tokens**: 196557
- **Prompt tokens**: 196554
- **Generation tokens**: 126
- **Generation seconds**: 396.719838332996
- **Load seconds**: 3.7901505830013775

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
| 65536 | start | 1 | 65519 | pass | 135.36750195900095 |
| 65536 | middle | 1 | 65519 | pass | 132.3193669579996 |
| 65536 | end | 1 | 65519 | pass | 129.03296941599547 |
