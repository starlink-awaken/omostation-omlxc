---
type: derived
source: projects/omlxc
owner: governance-team
last_updated: 2026-09-18
last-reviewed: 2026-09-18
---

# Project instructions

- Keep v3 work in `src/omlxc` and preserve `bin/omlx` unless a task explicitly
  owns legacy behavior.
- Write a failing test before production code; retain the 32 legacy tests as
  characterization coverage.
- Use Python 3.13, uv, Hatchling, Ruff, and Pyright strict.
- Maintain compute fabric capabilities: Priority QoS (`P0/P1/P2`), VRAM budget estimation,
  two-tier semantic caching, thermal/battery awareness, AST-based intent triage,
  0ms TTFT prefix pre-warming, and sliding-window context distillation (ADR-0192).
- Support CLI governance operations via `omlxc fabric inspect`, `omlxc fabric triage`,
  `omlxc fabric vram`, `omlxc fabric warm`, and `omlxc fabric compact`.
- Do not globally install `omlxc`, modify `/opt/homebrew/bin/omlxc`, contact
  real hardware from ordinary tests, or add public-release material.
- Keep personal configuration, secrets, model files, logs, and state out of git.
- Autonomous/daemon capabilities ship only with real-run evidence in the
  delivery note (a log line or emitted event actually observed). Green unit
  tests do not prove wiring: 2026-08-24 alone surfaced three "complete"
  capabilities that were dead on arrival — an unwired resident reconcile loop,
  a probe timeout mathematically too small, and a scanner module that threw
  `NameError` on first use behind a swallowed except. Every periodic mechanism
  must also touch a state file each cycle so `full-status.sh` heartbeat checks
  can detect silent death.
