# Context Capacity Benchmark Report

- **Model**: mlx-community/Qwen3.6-35B-A3B-4bit
- **Model identity SHA**: e1da56d9576a45f650c47834b1a4d15f0ed2d18b34caac456727731ff2c19433
- **Mode**: retrieve
- **Seed**: 20260727
- **Max generation tokens**: 512
- **Tolerance tokens**: 32

## Summary

- **Total cases**: 45
- **Passed cases**: 45
- **Highest all-pass tier**: 32768

## Token and time totals

- **Rendered prompt tokens**: 571338
- **Prompt tokens**: 571293
- **Generation tokens**: 1890
- **Generation seconds**: 859.188460168989
- **Load seconds**: 4.283749834001355

## Tier-by-position pass rate

| Tier | Position | Passed | Total | Rate |
| --- | --- | --- | --- | --- |
| 2048 | start | 3 | 3 | 1.0 |
| 2048 | middle | 3 | 3 | 1.0 |
| 2048 | end | 3 | 3 | 1.0 |
| 4096 | start | 3 | 3 | 1.0 |
| 4096 | middle | 3 | 3 | 1.0 |
| 4096 | end | 3 | 3 | 1.0 |
| 8192 | start | 3 | 3 | 1.0 |
| 8192 | middle | 3 | 3 | 1.0 |
| 8192 | end | 3 | 3 | 1.0 |
| 16384 | start | 3 | 3 | 1.0 |
| 16384 | middle | 3 | 3 | 1.0 |
| 16384 | end | 3 | 3 | 1.0 |
| 32768 | start | 3 | 3 | 1.0 |
| 32768 | middle | 3 | 3 | 1.0 |
| 32768 | end | 3 | 3 | 1.0 |

## Failure counts

- No failures recorded.

## Case details

| Tier | Position | Trial | Rendered tokens | Outcome | Generation seconds |
| --- | --- | --- | --- | --- | --- |
| 2048 | start | 1 | 2050 | pass | 4.12812891700014 |
| 2048 | start | 2 | 2050 | pass | 2.8964914999960456 |
| 2048 | start | 3 | 2050 | pass | 2.9265329170011682 |
| 2048 | middle | 1 | 2050 | pass | 3.111025874997722 |
| 2048 | middle | 2 | 2050 | pass | 3.2587381249977625 |
| 2048 | middle | 3 | 2050 | pass | 3.2585897089957143 |
| 2048 | end | 1 | 2050 | pass | 3.187770250005997 |
| 2048 | end | 2 | 2050 | pass | 3.295815750003385 |
| 2048 | end | 3 | 2050 | pass | 3.2275544590011123 |
| 4096 | start | 1 | 4091 | pass | 5.96342295899376 |
| 4096 | start | 2 | 4091 | pass | 5.83128962500632 |
| 4096 | start | 3 | 4091 | pass | 5.865865457999462 |
| 4096 | middle | 1 | 4091 | pass | 5.86972129199421 |
| 4096 | middle | 2 | 4091 | pass | 5.847622792003676 |
| 4096 | middle | 3 | 4091 | pass | 5.7851783340011025 |
| 4096 | end | 1 | 4091 | pass | 5.864913874997001 |
| 4096 | end | 2 | 4091 | pass | 5.797349833002954 |
| 4096 | end | 3 | 4091 | pass | 5.806195125005615 |
| 8192 | start | 1 | 8195 | pass | 11.301806499999657 |
| 8192 | start | 2 | 8195 | pass | 11.271389125002315 |
| 8192 | start | 3 | 8195 | pass | 11.133973291005532 |
| 8192 | middle | 1 | 8195 | pass | 11.028636041999562 |
| 8192 | middle | 2 | 8195 | pass | 11.01900154199393 |
| 8192 | middle | 3 | 8195 | pass | 10.99386170800426 |
| 8192 | end | 1 | 8195 | pass | 10.980403207999188 |
| 8192 | end | 2 | 8195 | pass | 11.0322949170004 |
| 8192 | end | 3 | 8195 | pass | 11.212978999996267 |
| 16384 | start | 1 | 16365 | pass | 23.415349291994062 |
| 16384 | start | 2 | 16365 | pass | 23.1668087500002 |
| 16384 | start | 3 | 16365 | pass | 23.144210792001104 |
| 16384 | middle | 1 | 16365 | pass | 23.599666750000324 |
| 16384 | middle | 2 | 16365 | pass | 23.46611029199994 |
| 16384 | middle | 3 | 16365 | pass | 29.515871083000093 |
| 16384 | end | 1 | 16365 | pass | 23.069916917003866 |
| 16384 | end | 2 | 16365 | pass | 21.956473082995217 |
| 16384 | end | 3 | 16365 | pass | 21.768345249998674 |
| 32768 | start | 1 | 32781 | pass | 52.048529583000345 |
| 32768 | start | 2 | 32781 | pass | 51.99144912499469 |
| 32768 | start | 3 | 32781 | pass | 51.262571457999 |
| 32768 | middle | 1 | 32781 | pass | 51.18836645800184 |
| 32768 | middle | 2 | 32781 | pass | 51.257477874998585 |
| 32768 | middle | 3 | 32781 | pass | 51.29329220899672 |
| 32768 | end | 1 | 32781 | pass | 51.22972783300065 |
| 32768 | end | 2 | 32781 | pass | 51.75161674999981 |
| 32768 | end | 3 | 32781 | pass | 52.16612454099959 |
