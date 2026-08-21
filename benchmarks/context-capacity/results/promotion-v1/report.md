# Context Capacity Benchmark Report

- **Model**: mlx-community/Qwen3.6-35B-A3B-4bit
- **Model identity SHA**: e1da56d9576a45f650c47834b1a4d15f0ed2d18b34caac456727731ff2c19433
- **Seed**: 20260727
- **Max generation tokens**: 512
- **Tolerance tokens**: 32

## Summary

- **Total cases**: 45
- **Passed cases**: 45
- **Highest all-pass tier**: 32768

## Token and time totals

- **Rendered prompt tokens**: 571347
- **Prompt tokens**: 571302
- **Generation tokens**: 1980
- **Generation seconds**: 1402.6187623309816
- **Load seconds**: 7.373009417002322

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
| 2048 | start | 1 | 2057 | pass | 9.907583000000159 |
| 2048 | start | 2 | 2057 | pass | 6.512610749996384 |
| 2048 | start | 3 | 2057 | pass | 5.791083500000241 |
| 2048 | middle | 1 | 2057 | pass | 5.9632241660001455 |
| 2048 | middle | 2 | 2057 | pass | 5.478454374999274 |
| 2048 | middle | 3 | 2057 | pass | 5.530131917003018 |
| 2048 | end | 1 | 2057 | pass | 5.762331958001596 |
| 2048 | end | 2 | 2057 | pass | 5.853146417000971 |
| 2048 | end | 3 | 2057 | pass | 5.769513708000886 |
| 4096 | start | 1 | 4099 | pass | 9.60559370899864 |
| 4096 | start | 2 | 4099 | pass | 9.8382816250014 |
| 4096 | start | 3 | 4099 | pass | 8.838556292001158 |
| 4096 | middle | 1 | 4099 | pass | 9.795319666998694 |
| 4096 | middle | 2 | 4099 | pass | 9.012667499999225 |
| 4096 | middle | 3 | 4099 | pass | 8.455253666994395 |
| 4096 | end | 1 | 4099 | pass | 8.988148124997679 |
| 4096 | end | 2 | 4099 | pass | 8.806272124995303 |
| 4096 | end | 3 | 4099 | pass | 8.574233584004105 |
| 8192 | start | 1 | 8203 | pass | 16.521679541001504 |
| 8192 | start | 2 | 8203 | pass | 18.226154957999825 |
| 8192 | start | 3 | 8203 | pass | 21.557551999998395 |
| 8192 | middle | 1 | 8203 | pass | 20.14063012499537 |
| 8192 | middle | 2 | 8203 | pass | 19.921592083002906 |
| 8192 | middle | 3 | 8203 | pass | 19.72627470899897 |
| 8192 | end | 1 | 8203 | pass | 20.407432416999654 |
| 8192 | end | 2 | 8203 | pass | 22.605562625001767 |
| 8192 | end | 3 | 8203 | pass | 21.071598124995944 |
| 16384 | start | 1 | 16373 | pass | 42.489629499999864 |
| 16384 | start | 2 | 16373 | pass | 39.91237454100337 |
| 16384 | start | 3 | 16373 | pass | 39.304801333004434 |
| 16384 | middle | 1 | 16373 | pass | 39.13049079099437 |
| 16384 | middle | 2 | 16373 | pass | 38.96798179100006 |
| 16384 | middle | 3 | 16373 | pass | 39.224275874999876 |
| 16384 | end | 1 | 16373 | pass | 37.989056915997935 |
| 16384 | end | 2 | 16373 | pass | 38.5245731670002 |
| 16384 | end | 3 | 16373 | pass | 38.04987454199727 |
| 32768 | start | 1 | 32751 | pass | 96.64783283299766 |
| 32768 | start | 2 | 32751 | pass | 97.42055204200005 |
| 32768 | start | 3 | 32751 | pass | 100.54231129099935 |
| 32768 | middle | 1 | 32751 | pass | 91.41093312500016 |
| 32768 | middle | 2 | 32751 | pass | 89.55563266700483 |
| 32768 | middle | 3 | 32751 | pass | 91.25459670800046 |
| 32768 | end | 1 | 32751 | pass | 59.61832737499935 |
| 32768 | end | 2 | 32751 | pass | 51.572234790997754 |
| 32768 | end | 3 | 32751 | pass | 52.34240037499694 |
