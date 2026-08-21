# Context Capacity Benchmark Report

- **Model**: mlx-community/Qwen3.6-35B-A3B-4bit
- **Model identity SHA**: e1da56d9576a45f650c47834b1a4d15f0ed2d18b34caac456727731ff2c19433
- **Mode**: copy
- **Decoys**: 0
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
- **Generation seconds**: 844.9916639210351
- **Load seconds**: 6.1427020839983015

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
| 2048 | start | 1 | 2057 | pass | 4.872972334000224 |
| 2048 | start | 2 | 2057 | pass | 2.9816449170029955 |
| 2048 | start | 3 | 2057 | pass | 3.065169417001016 |
| 2048 | middle | 1 | 2057 | pass | 3.0365988749981625 |
| 2048 | middle | 2 | 2057 | pass | 3.0326542080001673 |
| 2048 | middle | 3 | 2057 | pass | 3.0900200000032783 |
| 2048 | end | 1 | 2057 | pass | 3.0535105829985696 |
| 2048 | end | 2 | 2057 | pass | 3.1030144579999615 |
| 2048 | end | 3 | 2057 | pass | 3.0211328329969547 |
| 4096 | start | 1 | 4099 | pass | 5.366522875003284 |
| 4096 | start | 2 | 4099 | pass | 5.5331045420025475 |
| 4096 | start | 3 | 4099 | pass | 5.458525542002462 |
| 4096 | middle | 1 | 4099 | pass | 5.44525408300251 |
| 4096 | middle | 2 | 4099 | pass | 5.500933000002988 |
| 4096 | middle | 3 | 4099 | pass | 5.628466624999419 |
| 4096 | end | 1 | 4099 | pass | 5.660517833006452 |
| 4096 | end | 2 | 4099 | pass | 5.685323125006107 |
| 4096 | end | 3 | 4099 | pass | 5.626042042000336 |
| 8192 | start | 1 | 8203 | pass | 10.918051417000243 |
| 8192 | start | 2 | 8203 | pass | 11.094983540999237 |
| 8192 | start | 3 | 8203 | pass | 11.093873291996715 |
| 8192 | middle | 1 | 8203 | pass | 11.12278283300111 |
| 8192 | middle | 2 | 8203 | pass | 11.12992716699955 |
| 8192 | middle | 3 | 8203 | pass | 11.046225292004237 |
| 8192 | end | 1 | 8203 | pass | 11.02571450000687 |
| 8192 | end | 2 | 8203 | pass | 10.946031249994121 |
| 8192 | end | 3 | 8203 | pass | 11.03184579200024 |
| 16384 | start | 1 | 16373 | pass | 23.20480958400003 |
| 16384 | start | 2 | 16373 | pass | 22.82402054099657 |
| 16384 | start | 3 | 16373 | pass | 22.636778042004153 |
| 16384 | middle | 1 | 16373 | pass | 22.773892249999335 |
| 16384 | middle | 2 | 16373 | pass | 22.835757583001396 |
| 16384 | middle | 3 | 16373 | pass | 22.82272566699976 |
| 16384 | end | 1 | 16373 | pass | 22.87655979199917 |
| 16384 | end | 2 | 16373 | pass | 22.86059808400023 |
| 16384 | end | 3 | 16373 | pass | 23.191388916995493 |
| 32768 | start | 1 | 32751 | pass | 52.05807379200269 |
| 32768 | start | 2 | 32751 | pass | 51.60668391700165 |
| 32768 | start | 3 | 32751 | pass | 51.54514358299639 |
| 32768 | middle | 1 | 32751 | pass | 51.26308058400173 |
| 32768 | middle | 2 | 32751 | pass | 51.594321124997805 |
| 32768 | middle | 3 | 32751 | pass | 51.35787800000253 |
| 32768 | end | 1 | 32751 | pass | 50.840823625003395 |
| 32768 | end | 2 | 32751 | pass | 49.80359904200304 |
| 32768 | end | 3 | 32751 | pass | 50.32468741699995 |
