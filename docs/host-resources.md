# Execution machine resources

Issue [#191](https://github.com/15253a/meta_research/issues/191) adds a shared
resource panel to the home page and research workspace. Open **执行机器资源**
to read `GET /api/v1/runtime/resources` through the existing authenticated
HTTP session. This read does not select devices or change Quest configuration.

The panel reports the whole execution machine, including other work on that
machine. It does not attribute usage to the current Quest.

- CPU is the aggregate Linux `/proc/stat` utilization over the reported short
  sampling interval. Guest ticks are already included in user/nice time;
  idle includes IO wait.
- Memory is Linux `/proc/meminfo` `MemTotal - MemAvailable`, with the total
  shown in GiB.
- Each NVIDIA GPU is identified by UUID. `nvidia-smi` supplies utilization
  and used/total memory in MiB. A later observation replaces the device list,
  so device addition, removal and ordering changes do not reassign readings.

The response includes the CPU interval and separate memory/GPU observation
times, with seconds since the Unix epoch. The panel refreshes every five
seconds while open and visible. Closing it stops observation requests.

Missing metrics remain `null` and display as **缺测**. A confirmed empty GPU
query displays **未检测到 GPU 设备**; a failed query displays an unavailable
state. Measured zero stays zero. If HTTP observation fails, the panel labels
the retained sample as an old observation and the current state as unknown.
The next successful observation replaces it.

Host CPU/memory collection targets the project's Linux execution server.
Other operating systems return `platform_unsupported` rather than a process
measurement or a default zero.

Local verification uses the public HTTP boundary, injectable operating-system
readers and the existing Playwright functional project:

```sh
python -m pytest tests/test_runtime_resources.py -q
cd web
npm test
npm run build
npx playwright test --project=functional host-resources.spec.ts
```

Backend execution requires the existing Linux environment described in
[implementation-baseline.md](implementation-baseline.md). Hardware samples,
raw source comparisons and browser captures are dated implementation evidence
under `.scratch/implementation-191-20261008` in the handoff workspace. Injected
multi-device cases and actual multi-GPU measurements are reported separately.
