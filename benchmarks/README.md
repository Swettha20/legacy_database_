# Independent Oracle benchmark

Source: [Oracle sample schemas](https://github.com/oracle-samples/db-sample-schemas), pinned commit `6660bad68c07bd143430ace58565b3f727e17263`. Preserve `ORACLE_SAMPLE_LICENSE.txt`. The sample data/scripts are publicly licensed; Oracle Database itself is proprietary.

`setup.ps1` downloads the pinned archive and Oracle SQLcl 26.3 (SHA-256 checked), preserves original files, supplies installer prompts noninteractively, and validates all nine official base-table counts. It refuses to overwrite an existing SH schema. A valid existing schema is checked and reused.

The Oracle Free slim image lacks Oracle Text. Its `SUP_TEXT_IDX` index is omitted explicitly in the compatibility adaptation. No base-table data is modified or synthetically scaled. Our recorded installation initially stopped at that index; remaining supported official statements were executed separately. `results/source-installation.json` records that limitation rather than claiming an unmodified full-feature installer passed.

Nine tables total **1,063,396 rows**: SALES 918,843; COSTS 82,112; CUSTOMERS 55,500; TIMES 1,826; SUPPLEMENTARY_DEMOGRAPHICS 4,500; PROMOTIONS 503; PRODUCTS 72; COUNTRIES 35; CHANNELS 5. Source allocated segments were 279,642,112 bytes in the initial installation; segment allocation is not logical payload size.

Run from the repository root after `start_all`:

```powershell
.\benchmarks\setup.ps1
python benchmarks\run.py --runs 3
```

The host runner needs Python and requests, or use a compatible environment. The application itself only needs Docker. Credentials come from private `.env.stack`; they are not saved in benchmark results. Keep the `.runtime/benchmark` installer files private because automated prompt settings include the local sample password.

Each configuration runs one labelled warm-up and three measured migrations. COPY and batched INSERT use the same source data and final value/constraint/export-restore checks. Do not compare a load-only time with an end-to-end time. Raw status-response latency samples are taken while migration/verification is active; container CPU/memory readings are sampled roughly every ten seconds.

`run.py --methods copy --batches 1000 5000 10000 20000 --runs 3` performs the additional batch-size sweep. The default records one batch size (5,000) and two transfer methods; it does not claim every possible tuning configuration was tested. Worker concurrency remains one.

Results live in `results/local.json`; warm-ups have `measured: false`. Hardware, code/image provenance and source adaptations accompany the report. Local Docker shares the host with other software: these are practical local measurements, not a controlled dedicated-host laboratory test.
