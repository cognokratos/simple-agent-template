# Maintaining `main` and `rust-agent`

Two branches, one source of truth.

```text
main        = the canonical NAT implementation + the canonical documentation
rust-agent  = exactly ONE commit on top of main: the Rig + Rust implementation delta
book        = consumes documentation from main only
```

## Rules

1. **`main` is canonical.** Its NeMo Agent Toolkit agent is the recommended
   production reference. `rust-agent` is a comparative learning implementation,
   never presented as replacing it.
2. **All documentation lives on `main`.** `README.md` and everything under
   `docs/` describe the shared architecture and *both* implementations, and are
   byte-identical on the two branches. A documentation change — including one
   about the Rig implementation — is made on `main`.
3. **`rust-agent` is one commit.** It contains only what the Rust
   implementation needs: the `agent/` crate in place of the Python agent, and
   the build and check files that run it (its Compose service, Makefile
   targets, CI job, the source-wiring checks, the agent licence notice).
4. **Shared behaviour changes on `main` first.** The UI, gateway, Keycloak, MCP
   server, schema and seed data, evaluation harness and datasets, prompts and
   network topology are identical on both branches. A change to any of them
   lands on `main` and reaches `rust-agent` when it is rebuilt.
5. **Security fixes to shared architecture are ported before anything else.**
   An open security fix on `main` that `rust-agent` does not carry is a defect
   of `rust-agent`. A security property one agent has and the other lacks needs
   an explicit, documented decision ([NAT-VS-RIG.md](NAT-VS-RIG.md#deliberate-behavioural-differences)).

## Why one commit

`rust-agent` exists to be compared with `main`. The most useful thing it can
offer is a clean answer to *"what exactly is different?"*:

```bash
git diff main rust-agent        # the whole difference, and nothing else
```

The chronology of how the Rust implementation was built is not valuable to a
reader; a growing pile of commits and merges would hide the delta. So the
branch is kept as a single commit, regenerated over each new `main`:

```text
main:        A -- B -- C -- D
                             \
rust-agent:                   R

main later:  A -- B -- C -- D -- E -- F
                                       \
rust-agent:                             R'
```

`R'` is the same logical Rust delta, rebased onto the newer `main`.

> **Warning for contributors.** Updating `rust-agent` rewrites its single
> commit. Do not base independent long-lived work directly on `rust-agent`
> without expecting occasional history rewrites. Propose Rust changes as a
> branch against `rust-agent` and expect to rebase it; propose everything else,
> including every documentation change, against `main`.

## Updating `rust-agent` after `main` moves

```bash
git fetch origin
git checkout rust-agent
git branch backup/rust-agent-$(date +%Y%m%d) rust-agent   # recovery point
git rebase origin/main                                     # replay R onto the new main
# resolve conflicts in implementation files only; docs/ and README.md must
# never conflict, because rust-agent never changes them
make docs-parity static-check agent-check
git rev-list --count origin/main..rust-agent               # must print 1
git push --force-with-lease origin rust-agent
git fetch origin && make rust-agent-drift                  # the remote pair is consistent again
```

Changing the Rust implementation itself works the same way: commit the change
on top of `rust-agent`, then fold it into the single commit
(`git rebase -i origin/main` and `fixup`, or `git reset --soft origin/main` and
recommit with the same message), and push with `--force-with-lease`. Never use
an unrestricted `--force`, and never rewrite `main`.

Expect conflicts only where `main` changed a file the Rust commit also changes —
the agent's Compose service, the Makefile, CI, or a check that inspects agent
source. Resolve them in favour of the Rust implementation, then port the
*intent* of `main`'s change:

| `main` changed… | In the Rust commit… |
| --- | --- |
| `agent/config.yml` prompts | copy them byte for byte; `prompt_sha256` must stay equal on both branches |
| the agent's SSE vocabulary or routes | `agent/src/api/` and `agent/tests/contract.rs`; the contract document is already updated on `main` |
| a guardrail rule or pattern | `agent/src/guardrails/` and its tests |
| approval prompts, claims or the action registry | `agent/src/approval/`; the agent's cross-check against `mcp-server/src/approval.rs` fails until both agree |
| evaluation event names | `agent/src/agent/input_rail.rs`, `execution.rs`; `scripts/verify_security_sources.py` checks they stay recognisable |

## Checks that keep the model honest

| Check | Where | Fails when |
| --- | --- | --- |
| `make docs-parity` (`scripts/verify_docs_parity.py`) | `rust-agent` CI | `README.md` or anything in `docs/` differs from `origin/main` — the documentation was edited on the wrong branch, or `rust-agent` has not been rebased since `main`'s docs changed |
| `make docs-check` | both branches | a relative link or anchor does not resolve, or a `make` target does not exist, **on that branch's tree** |
| `make rust-agent-drift` (`scripts/verify_rust_agent_drift.py`) | the *rust-agent drift* workflow, on both branches | `origin/rust-agent` is not based on the current `origin/main`, is not exactly one commit ahead of it, or its `README.md`/`docs/` differ |
| `git rev-list --count origin/main..rust-agent` | by hand, before every push | the branch is not exactly one commit |

`make docs-check` running on both trees is what enforces the link rule below.

## Knowing when `rust-agent` is stale

`docs-parity` runs only when `rust-agent`'s own CI runs, so after a change to
`main` the branch could fall behind silently. The **rust-agent drift** workflow
([`.github/workflows/rust-agent-drift.yml`](../.github/workflows/rust-agent-drift.yml))
closes that gap. It compares `origin/main` with `origin/rust-agent` and asks
two questions:

* is `rust-agent` exactly one commit on top of the *current* `main`
  (`main` is its merge base and parent)?
* are `README.md` and `docs/` byte-identical?

It is detection only: it never rebases, rewrites or pushes anything.

| Trigger | When drift is found |
| --- | --- |
| push to `main` | a **warning** in the run summary: "rust-agent must now be regenerated onto the new main". The run stays green, because a push to `main` makes `rust-agent` stale by design until it is rebuilt; `main`'s own CI is unaffected |
| daily schedule (06:17 UTC), manual run | the run **fails**, so drift that outlives the update is visible in the Actions tab and notified like any failed scheduled run |
| push to `rust-agent` | the run **fails**: the rebuilt branch is still not one commit on the current `main` |

Run the same check locally with `make rust-agent-drift` after `git fetch`.

## Writing documentation that works from both branches

Because the same files are read on both branches:

* **Shared files → relative links.** The gateway, MCP server, UI, database,
  evaluation, scripts, Compose file, Makefile and `agent/config.yml` exist on
  both branches.
* **Implementation source → explicit branch links.** NAT source exists only on
  `main` (`https://github.com/cognokratos/simple-agent-template/blob/main/agent/src/nat_streaming_react/...`);
  Rust source only on `rust-agent`
  (`https://github.com/cognokratos/simple-agent-template/blob/rust-agent/agent/src/...`).
* **Name the implementation, not the branch you are on.** Write "on
  `rust-agent`, …" or "the Rig agent …", never "this branch …".
* **`make` targets** named in docs must exist on both Makefiles. The
  implementation-neutral targets (`agent-test`, `agent-check`, the `verify-*`
  family) exist on both and run that branch's agent checks.

## Upgrading Rig

Rig, `rig-agent`, `rig-rmcp`, `rmcp` and the OpenTelemetry crates are pinned
exactly in `agent/Cargo.toml` on `rust-agent`, and the source-wiring check fails
if the Rig crates are not. Rig reshapes its API between minor versions. To
upgrade, change the pins together, run `make agent-check`, and re-read the
release notes for hooks (`AgentHook::on_dispatch`, `on_invalid_tool_call`),
streaming items (`MultiTurnStreamItem`) and tool context — the three APIs the
policy layer depends on. Fold the upgrade into the single commit as above.

## The book

The CognoKratos book imports this template's documentation from `main` only. It
never fetches from `rust-agent`; the Rust material reaches it as part of
`main`'s documentation, and links into Rust source are ordinary hyperlinks to
`blob/rust-agent/...`. Maintaining the book therefore means caring only about
`main`.
