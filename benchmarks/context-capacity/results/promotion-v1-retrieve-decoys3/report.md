# Context Capacity Benchmark Report

- **Model**: mlx-community/Qwen3.6-35B-A3B-4bit
- **Model identity SHA**: e1da56d9576a45f650c47834b1a4d15f0ed2d18b34caac456727731ff2c19433
- **Mode**: retrieve
- **Decoys**: 3
- **Seed**: 20260727
- **Max generation tokens**: 512
- **Tolerance tokens**: 32

## Summary

- **Total cases**: 45
- **Passed cases**: 45
- **Highest all-pass tier**: 32768

## Token and time totals

- **Rendered prompt tokens**: 571581
- **Prompt tokens**: 571536
- **Generation tokens**: 1986
- **Generation seconds**: 829.4140188760139
- **Load seconds**: 3.8480015420063864

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
| 2048 | start | 1 | 2057 | pass | 4.671404375003476 |
| 2048 | start | 2 | 2057 | pass | 2.9208383330042125 |
| 2048 | start | 3 | 2057 | pass | 2.9325127499978407 |
| 2048 | middle | 1 | 2057 | pass | 2.9910040839968133 |
| 2048 | middle | 2 | 2057 | pass | 3.318133417000354 |
| 2048 | middle | 3 | 2057 | pass | 3.3734843749989523 |
| 2048 | end | 1 | 2057 | pass | 3.0235505000018748 |
| 2048 | end | 2 | 2057 | pass | 3.20393291599612 |
| 2048 | end | 3 | 2057 | pass | 2.9473201660002815 |
| 4096 | start | 1 | 4096 | pass | 5.384406875004061 |
| 4096 | start | 2 | 4096 | pass | 5.188769667001907 |
| 4096 | start | 3 | 4096 | pass | 5.285937874999945 |
| 4096 | middle | 1 | 4096 | pass | 5.289515791002486 |
| 4096 | middle | 2 | 4096 | pass | 5.368079291001777 |
| 4096 | middle | 3 | 4096 | pass | 5.3218052920055925 |
| 4096 | end | 1 | 4096 | pass | 5.314735792002466 |
| 4096 | end | 2 | 4096 | pass | 5.285780459002126 |
| 4096 | end | 3 | 4096 | pass | 5.280632792004326 |
| 8192 | start | 1 | 8200 | pass | 10.497242749996076 |
| 8192 | start | 2 | 8200 | pass | 10.44590370899823 |
| 8192 | start | 3 | 8200 | pass | 10.54123174999404 |
| 8192 | middle | 1 | 8200 | pass | 10.505532041999686 |
| 8192 | middle | 2 | 8200 | pass | 10.43283212499955 |
| 8192 | middle | 3 | 8200 | pass | 10.572495874999731 |
| 8192 | end | 1 | 8200 | pass | 10.688092582997342 |
| 8192 | end | 2 | 8200 | pass | 10.344778457998473 |
| 8192 | end | 3 | 8200 | pass | 10.388644874998135 |
| 16384 | start | 1 | 16370 | pass | 23.14581837500009 |
| 16384 | start | 2 | 16370 | pass | 23.120796167000663 |
| 16384 | start | 3 | 16370 | pass | 22.851394499994058 |
| 16384 | middle | 1 | 16370 | pass | 22.476766416999453 |
| 16384 | middle | 2 | 16370 | pass | 22.779089708004904 |
| 16384 | middle | 3 | 16370 | pass | 22.773869457996625 |
| 16384 | end | 1 | 16370 | pass | 22.60355020800489 |
| 16384 | end | 2 | 16370 | pass | 22.492582833001507 |
| 16384 | end | 3 | 16370 | pass | 22.639506584004266 |
| 32768 | start | 1 | 32786 | pass | 51.10262312499981 |
| 32768 | start | 2 | 32786 | pass | 50.60975341699668 |
| 32768 | start | 3 | 32786 | pass | 50.63390691699897 |
| 32768 | middle | 1 | 32786 | pass | 50.37603737500467 |
| 32768 | middle | 2 | 32786 | pass | 50.14333512500161 |
| 32768 | middle | 3 | 32786 | pass | 49.83896429200104 |
| 32768 | end | 1 | 32786 | pass | 50.43084512499627 |
| 32768 | end | 2 | 32786 | pass | 49.978938375003054 |
| 32768 | end | 3 | 32786 | pass | 49.89764195799944 |
