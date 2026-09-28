# snapctx

[![CI](https://github.com/snaptoolkit/snapctx/actions/workflows/ci.yml/badge.svg)](https://github.com/snaptoolkit/snapctx/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

*snapctx = **snap** **c**on**t**e**x**t — a snapshot of the context an agent needs.*

**Structured codebase context for AI agents.** One CLI call replaces the agent's usual `grep` + `read` + chase-imports loop. Ask a natural-language question; get back a self-contained pack of top symbols, their source, callees, callers, and module-level docstrings.

**Real-world agent usage (vs. default `grep`/`read`/`glob` tools):**

| | Regular tools | snapctx | Win |
|---|---|---|---|
| Tool calls per query | 6–10 | **1** | ~10× fewer |
| Wall-clock time | 15–30 s | **2–5 s** | ~10× faster |
| Tokens per query | ~70 k | **~7 k** | ~10× fewer |
| Token range | 40 k – 120 k | **6 k – 10 k** | ~10× fewer |
| Lines processed | ~5,800 | **~1,500** | ~4× fewer |
| Files accessed | 6 full reads | **4 ranked symbols** | targeted |
| Query mode | sequential | **parallel** | concurrent fan-out |

[See the controlled benchmark](#tool-benchmark) for per-query breakdowns. These figures describe that setup; the later [retrieval experiments](#retrieval-experiments) include contrasting agent results, skeleton approaches, and validation across three anonymized projects.

Languages today: Python, TypeScript, TSX, JSX, shell (`.sh`/`.bash`), Markdown, TOML, YAML, JSON, and `.env` files. Code is parsed structurally (symbols, calls, imports); docs and configs index headings / top-level keys so an agent can find them without grep. The parser layer is pluggable — adding a language is a single new file.

---

## Get started in 60 seconds

```bash
# 1. Clone and install (one time). Puts `snapctx` on your $PATH.
git clone https://github.com/snaptoolkit/snapctx.git
cd snapctx
uv tool install --editable .

# 2. Go to any code repo and ask a question. snapctx auto-builds the index on first use.
cd /path/to/your/repo
snapctx context "how does session authentication work"
```

That's it. You get JSON back: top-5 matching symbols, their full source, who they call, who calls them, file outlines for the surrounding files. Usually enough to answer a non-trivial question in one call.

Here's real output from running that query against the `requests` library (~370 ms cold CLI; under 10 ms with `snapctx watch` running):

```jsonc
{
  "query": "how does session authentication work",
  "mode": "hybrid",
  "seeds": [
    {
      "rank": 1,
      "qname": "requests.sessions:SessionRedirectMixin.rebuild_auth",
      "kind": "method",
      "signature": "def rebuild_auth(self, prepared_request, response)",
      "docstring": "When being redirected we may want to strip authentication from the request to avoid leaking credentials.",
      "file": "requests/sessions.py",
      "lines": "282-300",
      "score": 0.0479,
      "callees": [
        {
          "qname": "requests.sessions:SessionRedirectMixin.should_strip_auth",
          "signature": "def should_strip_auth(self, old_url, new_url)",
          "docstring": "Decide whether Authorization header should be removed when redirecting",
          "line": 290,
          "callees": [                              // depth-2 hop
            { "qname": "?:urlparse", "line": 130, "resolved": false }
          ]
        }
      ],
      "callers": [
        {
          "qname": "requests.sessions:SessionRedirectMixin.resolve_redirects",
          "signature": "def resolve_redirects(self, resp, req, …)",
          "docstring": "Receives a Response. Returns a generator of Responses or Requests.",
          "line": 246,
          "callers": [                              // depth-2 hop
            { "qname": "requests.sessions:Session.send", "line": 725 }
          ]
        }
      ],
      "source": "def rebuild_auth(self, prepared_request, response):\n    …"
    }
    // … 4 more seeds
  ],
  "file_outlines": [
    {
      "file": "requests/sessions.py",
      "symbols": [
        { "qname": "requests.sessions:merge_setting",   "kind": "function", "lines": "62-89"   },
        { "qname": "requests.sessions:SessionRedirectMixin", "kind": "class", "lines": "107-353" },
        { "qname": "requests.sessions:Session",         "kind": "class",    "lines": "354-833"  }
        // …
      ]
    }
  ],
  "token_estimate": 5552,
  "hint": "This response bundles search + callees + callers + top sources + a file outline. If it's still not enough, call `expand`, `outline`, or `source` on a specific qname."
}
```

**Requirements:** Python ≥ 3.11, [`uv`](https://github.com/astral-sh/uv) (or `pip install -e .` in a venv), ~200 MB disk for the ONNX embedding model (downloaded on first index).

**Uninstall:** `uv tool uninstall snapctx`.

---

## Tool benchmark

### Real-world agent usage

Aggregate over a session of agent-driven queries on a real backend
(opencode + Llama / Gemma, navigating an unfamiliar Django repo with
`snapctx_*` tools wired in alongside the defaults):

| Metric | Regular tools | snapctx | Factor |
|---|---|---|---|
| Tool calls per query | 6–10 | 1 | ~10× fewer |
| Round trips | 6–10 | 1 | ~10× fewer |
| Wall-clock time | 15–30 s | 2–5 s | ~10× faster |
| Tokens per query | ~70 k | ~7 k | ~10× fewer |
| Token range | 40 k – 120 k | 6 k – 10 k | ~10× fewer |
| Lines processed | ~5,800 | ~1,500 | ~4× fewer |
| Files accessed | 6 full files | 4 ranked symbols | 33% fewer, targeted |
| Query mode | sequential | parallel | concurrent fan-out |

Numbers rounded; full traces gathered across `context`, `search`,
`source`, and `expand` calls vs. the model's default
`grep`/`read`/`glob` loop.

### Refactoring

Same setup, but for write-side tasks (find usages, rename, update
imports). snapctx's symbol-aware writers (`edit_symbol`,
`edit_batch`, `rename_symbol`, `add_import`/`remove_import`,
`move_file`) make the agent's loop both shorter and safer:

| Metric | Regular tools | snapctx | Factor |
|---|---|---|---|
| Calls to understand the code | 15–20 | 4–5 | 3–4× fewer |
| Refactor wall-clock time | 60–120 s | 15–30 s | 4–8× faster |
| Refactor tokens | ~45 k | ~8 k | ~6× fewer |
| Find all usages | multiple `grep` passes | `snapctx_expand` | complete in one call |
| Rename symbol | manual multi-file edits | `snapctx_rename_symbol` | atomic across def + callers + imports |
| Update imports after move | manual | coordinated via `move_file` + `add_import`/`remove_import` | automatic |
| Risk of missed sites | high (regex / line-by-line) | low (call-graph + qname routing) | comprehensive |
| Confidence | medium | high (syntax pre-flight + per-file atomicity) | refuses to land broken edits |

### Controlled microbenchmark

Minimum-grep vs warm snapctx Python API (model pre-loaded, as with `snapctx watch`). Tokens estimated at 4 chars/token from actual bytes returned by tools — no agent reasoning included.

| Query | Type | Calls (grep → snapctx) | Speed | Tokens (grep → snapctx) | Token content |
|---|---|---|---|---|---|
| Search pipeline end-to-end | survey | 6 → 1 (**6× fewer**) | 34 ms → 6 ms (**6× faster**) | 13.8 k → 6.5 k (**2× fewer**) | grep reads whole files; snapctx returns filtered symbol bodies |
| Every SQLite connection open | audit | 4 → 2 (**2× fewer**) | 51 ms → 6 ms (**8× faster**) | 1.1 k → 2.4 k (2.2× more) | grep returns raw matched lines; snapctx returns qname + file + call-graph per hit |
| Multi-root discovery logic | architecture | 3 → 1 (**3× fewer**) | 16 ms → 5 ms (**3× faster**) | 4.2 k → 6.6 k (1.6× more) | grep reads two full files; snapctx includes call-graph depth and neighbors grep cannot produce |

Audit and architecture queries return more tokens from snapctx, but the content is structured — every hit already carries its enclosing `qname`, call-graph neighbors, and file outline. The agent synthesises directly without further reads. These are minimum grep counts; real agents make more exploratory calls, widening the call-count gap to 16×.

---

## Retrieval experiments

Study dates: 2026-09-26–2026-09-28.

This study compared ordinary source search, Graphify, Snapctx's read interfaces, skeleton-guided exploration, and Jev ranking through OpenRouter. It then optimized a **standalone experimental retriever** combining Snapctx, live ripgrep, and one Jev request. The worker described below is an external prototype: these results do **not** mean it is shipped in Snapctx, registered as an MCP tool, or enabled in an agent's configuration.

**Latest result:** 38 questions across three anonymized projects, run twice after stabilization. Reference-file coverage was **93.3–97.5%**, with **441–623 ms warm median latency**, approximately **9.7–10.0 kB returned per question**, and **$0.00037–$0.00038 Jev cost per question**. No previously retrieved reference file was lost in the final comparison. Six questions still required follow-up. These are retrieval measurements, not a new end-to-end agent accuracy or billing result.

### Scope, scenarios, and measurement

| Corpus | Indexed/scanned files | Questions | Scope |
|---|---:|---:|---|
| Project A | 2,045 | 18 | Backend, web frontend, CMS, mobile/web runtime, and deployment infrastructure |
| Project B | 829 | 10 | Backend, dashboard, media processing, authentication, and infrastructure |
| Project C | 13,688 | 10 | Backend, frontend, document processing, export/review workflows, infrastructure, and many documentation/output artifacts |

Questions and reference files came from actual commits and direct source inspection. Application source was read only; indexes were built or refreshed separately. Representative questions included:

- How does partial content availability affect frontend navigation and backend responses?
- Which frontend caches must be bypassed for generated content, and which backend endpoint serves it?
- How do cached mapping reads avoid an AI rate limit, and where is the batch route registered?
- How does OAuth state travel through kickoff, callback, and completion?
- Which runtime headers refresh callback entry assets while retaining ordinary static-asset caching?
- Where do CMS evaluation filters, request types, and backend handlers connect?
- How do EPUB headings, covers, anchors, and internal links get generated?
- Which prompt templates, batch metadata, parser rules, and release files affect a processing workflow?
- Where are a production feature gate, public sharing, invitation login, and protected exports implemented?
- How are subtitle edit caches invalidated, organization budgets enforced, pending review changes stored, and interrupted jobs resumed?

**Metrics:** reference-file recall@K is the mean, across questions, of the fraction of known implementation files present in the first K distinct returned paths. Reference sets are partial: an extra returned file may be relevant. Recall does not measure precision, snippet sufficiency, or correctness of a completed code change. The early agent study separately graded explanations against source.

Returned bytes measure agent-visible context. Token proxies below are estimates, sometimes using four bytes/characters per token; **Codex billed tokens and total model cost were not measured**. Jev costs are separately reported OpenRouter charges. kB means approximately 1,000 bytes; KiB means 1,024 bytes. Costs, timings, and rounded output figures describe the observed runs. The ranking prototype used `typesafe/jev-1.13` through OpenRouter's `/api/alpha/decisions` endpoint; this records the experiment configuration, not a promise of future API availability.

Native API queries used an existing index; some initial calls still paid embedding-model loading costs. Fresh-process wrapper timings include initialization. Warm JSONL medians exclude the first request per worker. Index building is excluded from query latency. Project A's original index build took 113 seconds including model download, extracting 22,266 symbols and creating 19,102 embeddings. Its native CLI freshness check made individual calls roughly 4–5 seconds in that workspace, unlike the already refreshed in-process API. Graph construction time was not measured. These measurements are separate from the earlier native-tool benchmarks above.

### 1. Early Graphify/Jev exploration

Three fresh agents first explored three cross-service questions with ordinary search, Graphify plus ripgrep, or Graphify plus ripgrep and Jev. This was exploratory: end-to-end timing and model tokens were not collected consistently.

| Approach | Context queries | Additional searches/reads | Observed elapsed | Jev cost |
|---|---:|---:|---|---:|
| Ordinary search | 0 | About 31 | 3–5 min | $0 |
| Graphify + ripgrep | 8 | About 35 | Not timed end to end | $0 |
| Graphify + ripgrep + Jev, first draft | 3 successful, 1 network failure | About 11 | 2–3 min, excluding permission delay | $0.001295 |

The three successful Jev queries took 6.555 seconds inside the tool, consuming 30,826 input and 1,926 output tokens. The first draft missed important files in two scenarios. Improvements retained short query terms, separated graph and text candidates, protected relevant repository representation, filtered unrelated generated/test/command paths, and selected implementation excerpts instead of imports. A fourth fresh agent used three revised queries (6.598 seconds total, $0.001287) plus 15 source reads/searches. Later excerpt fixes received direct-query and MCP protocol checks, but no further full agent run at that stage.

Splitting a combined authentication question into two focused flows took about 2.2 seconds and $0.00042–$0.00045 per query and improved entry-point selection. Repository names or similar authentication terms alone were insufficient evidence that two implementations belonged to one active call chain. Jev could reorder candidates but could not recover files missing from its input pool.

### 2. Three fresh Luna agents: same seven questions

Three agents received the same seven questions, with no access to commit history, answer sets, or each other's output. A used ordinary shell tools; B used Graphify locally and attempted Jev; C used Snapctx locally and attempted Jev. Both assisted agents encountered provider/network errors in their initial sessions. A later Graphify follow-up succeeded after environment/network setup; the separate retrieval benchmark below successfully used Jev on all seven questions.

| Agent workflow | Initial answer accuracy | Elapsed | Retrieved-context token proxy | Context reduction vs A | Historical score |
|---|---:|---:|---:|---:|---:|
| A: ordinary search | 7/7 = 100% | 52 s | ~6,250 | Baseline | 100/100 |
| B: Graphify, local + Jev attempted | 5.25/7 = 75% | 65 s | ~39,120 | −526% (more output) | 67/100 |
| C: Snapctx, local + Jev attempted | 3.5/7 = 50% | 107 s | ~52,000 | −732% (more output) | 44/100 |

The historical score was `60 × accuracy + 25 × (52 / elapsed_seconds) + 15 × (6250 / context_proxy)`. Accuracy was based on completeness of the cited mechanism, with partial credit for missing parts. The context estimates came from mixed-quality agent reports and truncated tool outputs; they are not comparable billing records. Large result dumps and provider retries inflated assisted-agent context. Follow-up troubleshooting was excluded from initial accuracy.

**The assisted agents did not save context or time in this run.** This is not a clean successful-Jev-versus-local ablation, and later retrieval improvements were not followed by another equivalent three-agent evaluation. Do not reuse these scores as scores for the stabilized worker.

### 3. Native read interfaces and initial rankers

Seven broad questions from Project A; native search/context through the Python API, external rankers through their wrappers. Latency and output are means. `--also` used two manually guided facets, so it had additional query information.

| Method | Recall@8 | Latency | Returned output | Jev cost for 7 |
|---|---:|---:|---:|---:|
| Lexical search | 0.388 | 76 ms | 4.1 kB | $0 |
| Vector search | 0.452 | 137 ms | 3.9 kB | $0 |
| Hybrid search | 0.493 | 49 ms | 4.1 kB | $0 |
| Hybrid with source bodies | 0.493 | 59 ms | 9.2 kB | $0 |
| Hybrid with two guided `--also` facets | 0.624 | 112 ms | 5.9 kB | $0 |
| Default `context` | 0.502 | 54 ms | 12.3 kB | $0 |
| `context`, 3 seeds / 2 bodies / 4 outlines | 0.436 | 54 ms | 18.9 kB | $0 |
| `context`, 8 seeds / 4 bodies / 12 outlines | 0.464 | 57 ms | 11.0 kB | $0 |
| Vector `context` | 0.519 | 37 ms | 11.3 kB | $0 |
| Graphify + local lexical ranking | 0.667 | 2,404 ms | 11.2 kB | $0 |
| Initial Snapctx + live ripgrep + one Jev call | 0.824 | 2,033 ms | 7.1 kB | $0.00253 |
| Focused skeleton + repeated Jev + source | 0.748 | 3,007 ms | 11.1 kB | $0.00350 |
| Graphify + Jev | 0.860 | 2,991 ms | 11.1 kB | $0.00299 |

A separate initial hybrid run averaged 46 ms at the same 0.493 recall. The focused-skeleton repeat reached 0.795 recall; borderline Jev rankings varied. Smaller seed/body settings did not ensure smaller total output because outlines and other response fields also contributed.

Generated output, tests, migrations, and unrelated parser code crowded top results. Overfetching 100 hybrid hits and filtering generated/test paths raised recall to **0.605**, with approximately **3.5 kB** output and **70 ms median** latency. Adding guided facets to that filtered variant still yielded 0.605 in this run. Means were 193 ms and 142 ms respectively, including initial-load effects. Narrowing to `context` on just three selected symbols produced **0.436 recall**, 69 ms mean latency, and 16.6 kB output: relevant files were discarded too early. Source bodies and larger context packs did not fix discovery gaps.

Exact names and structural operations answered narrower questions:

| Interface | Observed timing and output | Finding |
|---|---|---|
| Scoped `find_literal` | ~15 ms; 1.9 kB | Exact target found in 7/7 examples |
| Scoped `rg -F -l` | ~16 ms; 0.2 kB | Exact target found in 7/7 examples, including files without symbols |
| Native `grep --in` | ~4.4 s; 1.1 kB | Raw text coverage with symbol annotations, but expensive here |
| Exact-qname `source` | 0.7–1.3 ms; 1.6–2.7 kB | Efficient selected-source read |
| `expand`, both directions, depth 2 | 0.9–1.5 ms; 0.2–5.3 kB | Efficient known-symbol relationship lookup |
| File `outline` | 0.7–2.4 ms; 0.4–28 kB | Output depends heavily on file size and parsed symbols |
| Scoped `map` | 5–18 ms; 9–49 kB | Useful subtree orientation; can still be large |
| `routes` API | ~1 ms; 13 kB for 60 routes | Useful route inventory, with incomplete framework coverage |

The initial `find_literal` harness used a relative scope against absolute indexed paths and returned no matches. Correcting the scope to an absolute path yielded the 7/7 exact-target result above. One literal still achieved only **0.531 full-workflow file recall**: finding an entry point does not complete a cross-file trace. Native `grep` can include `.env` files; explicitly scope raw text searches to the intended source area. Write interfaces were outside this retrieval study.

### 4. Full skeleton, smaller skeleton, and incremental dives

- **Full compact skeleton:** 3.88 million characters across 1,406 symbol-bearing files in Project A. A minimal rendering was estimated at 1.49 million characters. Truncation at 20,000 characters covered only about the first 104 files in directory order, not a balanced sample. Symbol-free runtime files were missing, requiring a live path inventory.
- **Hierarchical Jev exploration:** repository area → directory → file → symbol → source, tested on three questions. It needed **17–21 Jev requests**, **6–8 seconds**, **65–75k Jev input tokens**, and **$0.0027–$0.0032 per question**. Adding missing paths recovered one runtime server, but another question followed the wrong processing area.
- **Focused skeleton:** build the skeleton only for Snapctx/live-search candidates, then let Jev narrow files and symbols. It used **3–4 Jev requests per question**, about **3 seconds**, and **11.1 kB** returned output. More precise symbol selection did not beat the single-ranking approach on overall file coverage, latency, or cost.
- **Fixed source enrichment:** append full source for the first three ranked symbols and one-hop neighbors while retaining 12 candidates. Five questions took **1.9–2.9 seconds** with **12–16 kB** output. File discovery did not improve, and the first three symbols were sometimes the wrong sources to prioritize.

The useful incremental strategy was to retain compact candidates, inspect selected source, and follow exact imports, routes, constants, or callers. Preloading the whole skeleton and automatically attaching bodies consumed context without establishing better completeness.

### 5. Broader scenarios and candidate-pool fixes

The original seven questions grew to 13 development questions, followed by five held-out questions. On the 13-question set, mean recall@8 was 0.509 for hybrid search, 0.533 for default context, 0.512 for the original local combined pool, 0.726 for that pool plus Jev, 0.710 for focused dives, and 0.771 for Graphify plus Jev. Mean latencies were 116, 53, 1,552, 1,995, 3,183, and 2,876 ms respectively.

Candidate generation had excluded relevant parser areas and missed hidden workflows, extensionless Dockerfiles, unparsed runtime code, and source HTML templates. The revised pool retained up to 20 initial symbol entries, limited symbols per file, filled remaining slots with live candidates, and capped the pool at 36. Two development runs of the revised ranker reached **0.899 and 0.890 recall@8**, with **1.921 and 1.977 second medians**, about **7.3 kB** output, and about **$0.00485 per 13 questions**.

| Five-question holdout | Recall@8 |
|---|---:|
| Native hybrid | 0.667 |
| Default context | 0.667 |
| Original combined pool + Jev | 0.400 |
| Revised combined pool + Jev | 0.933 |
| Graphify + Jev | 0.300 |

The revised holdout run averaged 1.98 seconds, 6.9 kB, and $0.00184 total. These five cases covered export covers, saved batch metadata, parsing names, OAuth errors, and language-specific prompt templates. Template support was subsequently adjusted after inspecting a remaining miss; later results on these same cases are regression measurements, not untouched holdout estimates.

Across all 18 questions:

| Method / prototype revision | Return budget | Mean recall | Median elapsed | Mean output | Jev cost for 18 |
|---|---:|---:|---:|---:|---:|
| Native hybrid | 8 paths | 0.553 | 54 ms | 3.9 KiB | $0 |
| Native context | 8 seeds | 0.570 | 56 ms | 14.3 KiB | $0 |
| Original Snapctx + live `rg` + Jev | 8 paths | 0.635 | 1,964 ms | 6.6 KiB | $0.00660 |
| Graphify + Jev | 8 paths | 0.640 | 2,853 ms | 10.3 KiB | $0.00765 |
| Revised local pool (v2) | 12 paths | 0.770 | 1,511 ms | 8.8 KiB | $0 |
| Single-scan local pool (v4) | 12 paths | 0.770 | 678 ms | 8.8 KiB | $0 |
| Revised pool + Jev (v2) | 10 paths | 0.945 | 1,971 ms | 8.9 KiB | $0.00668 |
| Revised pool + Jev (v2) | 12 paths | 0.964 | 1,954 ms | 10.6 KiB | $0.00668 |
| Single-scan pool + Jev (v4) | 12 paths | 0.964 | 1,112 ms | 10.5 KiB | $0.00669 |

These revision labels identify experiment iterations, not Snapctx releases. Different return budgets are not equivalent comparisons. The first eight paths of the revised 12-path run achieved **0.883 recall**. Returning 12 paths fully covered **16/18 questions**, without another Jev request, at about 1.7 KiB more output than the separate 10-path run. Rank ordering can vary between API calls.

Manual exact-name follow-ups recovered missing callers, throttle definitions, and route registrations in **8–29 ms**. These post hoc searches were not automated and are **not counted as 100% retrieval accuracy**.

### 6. Latency optimization and stability

The selected prototype retained a maximum of **36 candidates**, **650-character evidence excerpts**, **one Jev request**, and **12 distinct returned paths**. Optimization changed execution and serialization rather than adding another AI decision stage.

| Experiment | Observation | Decision |
|---|---|---|
| Ten parallel ripgrep processes (v3) | Four-query local median 1,541 → 1,427 ms, about 7% faster | Superseded by one scan |
| One multi-pattern ripgrep scan + local scoring (v4) | Same candidate sets over 18 questions, one ordering difference; local median 1,511 → 678 ms | Retained |
| Concurrent local retrieval in fresh processes (v5) | Contemporary median 1,178 → 1,219 ms | Cold concurrency alone offered no gain |
| Persistent JSONL worker | Initial warm median 684 ms at unchanged 0.964 recall | Retained model reuse |
| Overlap Snapctx and live text retrieval | Two order-reversed rounds: local median 338 → 232 ms; 36/36 identical ordered candidate evidence after deterministic ties | Retained |
| Reuse HTTP connection | 12 paired requests: median about 370 → 339 ms | Optional HTTPX pool; urllib fallback |
| Compact JSON | Mean output 10,902 → 9,731 bytes, all fields retained | About 11% fewer bytes, not measured billed-token savings |

Two subsequent 18-question runs reached **0.964 recall**, with **16/18 fully covered in each**, **645 ms combined warm median**, a **1,406 ms first request**, and approximately **$0.00037 per question**. Warm requests were about **45% faster** than the contemporary 1,178 ms fresh-process baseline. This comparison includes the benefit of keeping a process alive; it does not establish a 45% speedup for one isolated cold query.

The worker caches the embedding model and HTTP connection, **not retrieval results or source content**. Index freshness remains a separate responsibility. Stabilization introduced deterministic ties, default 12-path output, bounded/validated requests and roots, finite API scores in the range 0–1, malformed-JSONL recovery, connection reset on network failure, live-edit/deletion handling, and symlink boundaries. Failed live search produces an explicit error. Jev failure returns local results marked `jev.used: false` with a reason; local fallback has lower measured recall. A CLI `--no-jev` setting cannot be overridden by a JSONL request. The initial 11 focused checks grew to 15 after portability fixes.

### 7. Cross-project validation and fixes

Project A retained its 18 questions. Projects B and C each added eight development and two held-out questions. The final version ran all **38 questions twice: 76 queries**. The baseline here was the preceding stabilized worker, not the original native search or first prototype.

| Corpus | Questions | Previous recall@12 | Final recall@12 | Previous warm median | Final warm median | Fully covered |
|---|---:|---:|---:|---:|---:|---:|
| Project A | 18 | 96.4% | **96.4%** | 622 ms | **623 ms** | 16/18 |
| Project B | 10 | 70.0% | **93.3%** | 455 ms | **441 ms** | 7/10 |
| Project C | 10 | 84.2% | **97.5%** | 532 ms | **532 ms** | 9/10 |

The eight-question development subsets improved from **0.625 to 0.917** in Project B and **0.802 to 0.969** in Project C. Fixes addressed:

1. **Folder-name assumptions:** words such as “frontend” and “backend” had incorrectly restricted search to literally named folders. Search now honors the configured root without that inference.
2. **Infrastructure omissions:** discovery now covers root/nested Dockerfiles and variants, hidden GitHub workflows, GitLab CI, nginx `.conf` files, and runtime entry files.
3. **A regression caused by broader discovery:** runtime code competed with infrastructure config, and an unrelated deeper server displaced a needed runtime helper. Separate code/config slots and preservation of helpers referenced by relevant container images restored Project A's release scenario. Its temporary development recall drop from 0.964 to 0.950 was eliminated.
4. **A hardcoded default root:** the default became the current directory, with explicit root/environment overrides.

Both final runs had identical per-question reference coverage. Comparing actual retrieved reference-file sets found **zero lost files across all 76 final queries**. All queries successfully used Jev, returned valid paths within the configured root, and respected the 12-path cap. Mean returned bytes were **9,817 / 9,722 / 10,012** for A/B/C; mean Jev cost was approximately **$0.000373 / $0.000380 / $0.000381** respectively. **All 15 focused stability checks passed.**

The four held-out portability questions—subtitle cache invalidation, organization budgets, pending review changes, and interrupted-job checkpoints—were fully covered before and after fixes in both repeats. This is a small holdout, not evidence of universal completeness.

Cold starts were checked separately with three fresh-process queries per version per project, alternating version order:

| Corpus | Previous cold median | Final cold median |
|---|---:|---:|
| Project A | 1,228 ms | 1,150 ms |
| Project B | 819 ms | 844 ms |
| Project C | 927 ms | 914 ms |

One first Project A request in the main suite took 2.39 seconds; it did not recur in the cold checks, whose final samples ranged from 1,149 to 1,288 ms. Three samples per condition do not establish a latency guarantee.

**Remaining gaps:** six questions missed a shared feature flag, public-share route registration, a login API facade, a shared authenticated API base, a parent navigation page, or throttle/URL definitions. Exact-name follow-up remains necessary; the final worker does not automatically provide complete context for every scenario.

### Native improvements evaluated after the standalone experiments

The native tool now reuses vector matrices within a process, and offers an optional import/route follow-up. This is separate from the external Snapctx + ripgrep + Jev worker described above. No Jev call or agent configuration change is needed for these native features.

**Vector reuse is automatic.** A bounded process cache retains immutable matrices across API calls and checks SQLite commit versions through dedicated read-only observers. Writes from the same or another connection invalidate reuse; explicit transactions bypass it, and database replacement is detected. The cache retains at most three indexes and approximately 128 MiB of matrix/name data. It does not cache source text, query results, or embeddings for new queries. A fresh process still pays initialization and matrix-loading costs.

**Dependency follow-up is opt-in:**

```bash
snapctx context "how is a download authorized and routed" --related-file-limit 4
```

```python
from snapctx.api import context, related_files, related_files_multi

pack = context("download authorization", root="/path/to/repo", related_file_limit=4)
extra = related_files(["app.views:download"], root="/path/to/repo", limit=4)
```

The `related_files` response lists additional paths with evidence identifying the originating symbol, import or route-registration line, and referenced name. It follows one hop of Python imports (including selected function-local imports), relative JavaScript/TypeScript imports, and common local `tsconfig` alias mappings, including referenced configs. Unknown or ambiguous imports are skipped. Referenced source files must remain inside the root and match their indexed hashes; stale/deleted files and external symlinks do not produce new evidence. The supplement contains file pointers, not full source bodies or a guarantee that each dependency is relevant to the entire question. Read selected files afterward.

The default limit is **0**, preserving existing context output. Allowed limits are 0–16. The supplement is added after core payload trimming so it cannot evict existing outlines; its output is additional to the core token budget. Multi-root context also accepts the limit and tags supporting files with their root. No recursive import expansion or guessed cross-service relationship is added.

#### Native evaluation: 44 questions, three repetitions

The existing 38 questions were supplemented with six additional source-verified scenarios covering session identity, multipart-upload cleanup, export quality checks, provider-client construction, shared pronunciation caches, and localized section boundaries. Each revision ran in a separate process against the same existing indexes. The baseline was commit `6fcc6c5`; warmup was excluded. There were **264 baseline calls** (search and context) and **396 candidate calls** (search, context, and optional follow-up).

| Corpus | Questions | Native search median, before → after | Default context median, before → after | Default context coverage → with follow-up | Follow-up context median |
|---|---:|---:|---:|---:|---:|
| Project A | 20 | 45.8 → **20.7 ms** | 48.6 → **22.1 ms** | 56.3% → **66.3%** | 27.6 ms |
| Project B | 12 | 11.5 → **7.9 ms** | 15.5 → **10.5 ms** | 39.6% → **39.6%** | 25.0 ms |
| Project C | 12 | 42.7 → **23.6 ms** | 46.5 → **25.3 ms** | 68.1% → **75.0%** | 41.7 ms |

All **264 default response hashes matched** their baseline counterparts after canonical JSON serialization: rankings, source context, coverage, and output size were unchanged. Warm search was **31–55% faster**, and default context was **32–55% faster**. These are in-process API timings, not cold CLI or end-to-end agent timings.

Optional follow-up added an average **468–542 bytes** per question (about 2.8–3.5% more output), with no lost reference files. Across the original 38 questions, native context coverage rose from **53.6% to 57.5%**, and fully covered cases from **13 to 14**. Across the six additional questions, coverage rose from **63.9% to 86.1%**, and fully covered cases from **2 to 4**. The small additional set is exploratory. Project B showed no reference-coverage gain and paid extra latency, which is why follow-up is optional.

This coverage measure examines distinct files throughout the context payload; it is **not recall@12** and should not be directly compared with the earlier external-worker table. Additional files are structurally supported, but precision and full answer correctness were not graded. Native default token savings are **zero** here: identical context is returned faster. These changes do not establish new Codex billing savings or a replacement for the stronger external discovery worker.

Regression checks cover cache reuse, committed writes, deletion, empty indexes, transaction isolation, eviction, database replacement, concurrent readers, relative/configured imports, route evidence, ambiguity, stale files, symlink boundaries, output limits, multi-root routing, and preservation of core context at a token-budget boundary.

**All 501 tests passed**, including both opt-in performance checks.

A reusable [benchmark runner](benchmarks/native_retrieval.py) accepts an external scenario manifest; the [anonymized result summary](benchmarks/native-results-2026-09-28.json) records aggregate metrics. Run both revisions with the same Python environment, source snapshots, and refreshed indexes; add `--related` only for the new revision. Private manifests and source paths are intentionally not included.

### Recommended retrieval workflow and limits

1. **Known identifier, route, header, or literal:** start with scoped `find` or `rg -F -l`; read selected qnames with `source`, and use `expand` for callers/callees. This avoids a ranking API call.
2. **Broad question:** the strongest measured prototype used a bounded hybrid/live candidate pool, one Jev rank, and 12 compact paths. Keep its JSONL process alive for repeated queries; maintain index freshness separately.
3. **Incremental deepening:** inspect the selected implementation and follow exact imports, constants, routes, and callers locally. Use another model-ranking stage only when the missing component cannot be resolved that way.
4. **Orientation:** scope `map` or `outline` to a relevant subtree/file. A full skeleton was too large and incomplete for routine preload in this study.
5. **Alternative graph discovery:** Graphify remains an option when relevant areas are absent from the candidate pool. It won the initial seven-question comparison but lost to the revised retriever on the broader corpus; neither result proves a universal ranking.

Jev ranking sends bounded candidate excerpts to OpenRouter. Local-only retrieval has no Jev charge and avoids that source transfer. Native Snapctx and the external Jev experiment have different network behavior.

The measured gains are file coverage and retrieval latency. Output reduction is not an established overall token or dollar saving, and no new end-to-end agent score was measured after stabilization. The private corpora and raw traces are not distributed in this repository; the tables document observed results, while exact independent reproduction requires equivalent source snapshots, question/reference sets, tool versions, and provider settings. The experiment did not change application code or enable the standalone worker in Codex/MCP.

---

## The commands

You'll mostly use **`context`**. The others let you drill in when `context` isn't enough; **`find`** is the right tool when you need exhaustive grep-style coverage instead of ranked top-K.

| Command | What it does | When to use |
|---|---|---|
| `snapctx context "query"` | Everything-in-one-call: search + callees + callers + source + outlines. **Audit-aware**: when the query is an unambiguous audit phrasing (e.g. "audit every `transaction.atomic` site"), also runs `find` on the literal and attaches an exhaustive `find_results` block. | First move for any question. ~3–10 k tokens back. |
| `snapctx search "query"` | Top-K ranked symbols with signatures. Add `--with-bodies` to inline source. Add `--also <term2> [...]` to batch related terms in one call. | Ranked discovery; `--with-bodies` for one-shot audits when ≤ K hits. |
| `snapctx find "<literal>"` | **Exhaustive** literal-substring enumeration over every indexed symbol body. Returns ALL matches (not ranked, not capped). Add `--with-bodies` to inline containing-symbol source; add `--with-callers` to attach depth-1 callers (deduped) to every hit. | "Every place that uses X" audits — matches grep coverage with structured output. `--with-callers` turns it into "every site AND who triggers them" in one call. |
| `snapctx grep "<pattern>"` | Literal (default) or `--regex` search over **every** text file under the root — markdown, configs (TOML/YAML/JSON/.env), code, docs, plain text. Walks with the same gitignore + vendor + binary filters the indexer uses. Code-file hits are annotated with the enclosing-symbol qname so the agent can pivot to `snapctx source <qname>`. `-i` for case-insensitive, `--in <path>` to narrow, `-C N` for context lines. | Replaces the agent's external `grep`/`rg`. Closes the gap that `find` (symbol-body-only) leaves on README/configs/comments. |
| `snapctx map [--prefix PATH] [--depth 1\|2]` | Repo-wide table of contents — every indexed file's top-level symbols (signature, 1-line docstring, decorators), grouped by directory. No query needed. `--depth 2` also pulls in direct children (class methods). `--prefix` scopes to a sub-tree (e.g. `src/`). | Orientation when you don't yet have a specific question — fresh repo, unfamiliar area. Pairs with `search` for the actual lookup once you've oriented. ~6–30 k tokens depending on scope. |
| `snapctx outline path/` | Symbol tree of a file or directory (functions / classes / constants, nested). Add `--with-bodies` to inline source for every symbol. | Cheaper than reading whole files; directory mode gives you a module map. |
| `snapctx source <qname>` | Full body of a single symbol. Add `--with-neighbors` for resolved callee signatures. | When you have an exact qname and want its source. |
| `snapctx expand <qname>` | Walk the call graph. `--direction callees \| callers \| both`, `--depth 1 \| 2`. | "Who calls this?" / "What does this depend on?" |
| `snapctx edit <qname> <body_file>` | **Write op**. Replace a symbol's body by qname (re-indexes the file before returning). `--stdin` reads the body from stdin instead of a file. **Auto-recovery**: if the file's SHA drifted since indexing (autoformat-on-save, IDE write, parallel tool), the file is re-parsed in place and the edit proceeds against the fresh line range — the caller no longer has to re-query between same-file edits. **Syntax pre-flight**: refuses Python (`ast.parse`) or TS/TSX (tree-sitter) edits that would leave the file unparseable. The qname can also be a module-level address (e.g. `app.urls:` — empty after the colon) to replace the whole file's text. | Editing a function or class without re-reading the whole file. Pair with `source` (to see what's there) → write the replacement → `edit`. |
| `snapctx insert <anchor_qname> <body_file> [--position before\|after]` | **Write op**. Insert a NEW top-level symbol adjacent to an anchor symbol. Same staleness + syntax guards as `edit`. | Adding a new function or class without rewriting the whole file. |
| `snapctx delete <qname>` | **Write op**. Remove a symbol entirely. Drops the line range and trims one leading blank line so PEP-8 / Prettier spacing between top-level fns survives the deletion. | Clean removal — `edit_symbol(q, "")` would corrupt surrounding spacing. |
| `snapctx import-add <file> "<statement>"` / `snapctx import-remove <file> "<statement>"` | **Write op**. Add or remove an import line. Idempotent (`already_present` / `already_absent` in the response). Imports live above the first symbol and aren't reachable via `edit_symbol`; these fill that gap. For Python, `import-add` is docstring-aware — the new line lands AFTER a leading module docstring, not above it. | Updating imports after a rename, adding a new dependency, removing dead imports. |
| Python API: `edit_symbol_batch(edits, root)` | **Write op (Python API only)**. Apply N edits in one call, grouped by file, per-file atomic. A syntax error in any edit on a file rolls back THAT file; other files in the batch land independently. One re-index for the whole batch. | Multi-symbol refactors (rename across N callers, add validation to N functions). |
| Python API: `create_file(path, content, root)` / `delete_file(path, root)` / `move_file(old, new, root)` | File-level write ops. `create_file` writes a new file + reindexes; `delete_file` unlinks + drops symbols from the index; `move_file` renames + reindexes and returns `importing_files` so the caller can rewrite imports via `import-add` / `import-remove`. | Adding / removing / moving whole modules. |

The query can be:
- **Natural language**: `"how does rate limiting work"`, `"where do we verify credentials"`
- **An identifier**: `"SessionManager"`, `"verify_credentials"`
- **An exact qname** (fastest path, ~30 ms): `"app.auth:SessionManager.refresh"`

Add `--kind function|method|class|component|constant|interface|type` to any of these to narrow results.

---

## What just works (no setup)

- **Auto-indexing on first query.** Run `snapctx context …` in a fresh repo and it builds the index transparently before answering.
- **Forgiving qnames.** `source` and `expand` tolerate common LLM paraphrases — keep `.tsx`/`.py` on the module (`components/Verse.tsx:Verse`), apply Python dotted style to a TS file (`components.Verse:Verse`), or vice versa — and the call still resolves. The response includes a `paraphrase_hint` field naming the canonical form so the caller learns it for next time.
- **Auto-refresh on every query.** Subsequent runs incrementally re-index files whose SHA changed — usually <200 ms.
- **No `--root` flag.** snapctx walks up from your current directory to find the nearest `.snapctx/index.db`. Run from a deeply nested file; queries still hit the right index.
- **Stderr for progress, stdout for JSON.** Pipe stdout to `jq` without it choking on log lines.

To pre-build explicitly (one-time, ~5–10 s for a few hundred files):

```bash
snapctx index /path/to/your/repo
```

---

## Use it from your AI agent

snapctx is a CLI tool. Any agent that can run shell commands (Claude Code, opencode, Cursor's terminal, custom Agent SDK loops) can use it via `Bash` calls. Most agents default to `Grep` + `Read` + `Edit` out of habit — paste the block below into your project's `CLAUDE.md` / `AGENTS.md` (or your global one at `~/.config/opencode/AGENTS.md`, `~/.claude/CLAUDE.md`) to redirect them.

> **Using opencode?** A drop-in integration with all 18 snapctx ops wired as native tools (no MCP) lives in [`opencode/`](opencode/). Symlink the files into `~/.config/opencode/`, restart, done.

The block is language-agnostic. snapctx supports the same operations across **Python, TypeScript, TSX, JSX, shell, Markdown, TOML, YAML, JSON, and `.env`** — every command below works the same regardless of which language the file is in.

```markdown
## snapctx — your code-navigation and code-manipulation toolbox

This repo has a `.snapctx/index.db` (auto-built on first query). snapctx parses the codebase into a symbol graph and exposes both **read** ops (search / inspect) and **write** ops (edit / insert / rename / move). Prefer it over `Grep`, `Read`, `Glob`, `Edit`, and `Write` for anything code-related — fewer tool calls, fewer tokens, qname-addressed edits with syntax pre-flight.

### What snapctx parses

| Language | Extensions | Symbols extracted | Calls / imports |
|---|---|---|---|
| Python | `.py`, `.pyi` | functions, methods, classes, constants, modules | yes (call graph + import graph) |
| TypeScript / TSX | `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs` | functions, methods, classes, components, types, interfaces, constants | yes (call graph + import graph) |
| Shell | `.sh`, `.bash` | functions, module-level | intra-file calls + `source`/`.` imports |
| Markdown | `.md`, `.markdown` | headings (nested as qnames) | — |
| TOML | `.toml` | top-level keys + table headers | — |
| YAML | `.yaml`, `.yml` | top-level keys | — |
| JSON | `.json` | top-level keys | — |
| Env | `.env` | `KEY=value` variables | — |

Every symbol has a stable **qname** of the form `<module-path>:<member-path>` (Python: `app.auth:SessionManager.refresh`; TS: `src/auth/session:SessionManager.refresh`; Markdown: `README.md:Setup.Quickstart`; TOML: `pyproject.toml:project.version`).

### Read ops — pick by question shape

| Question | Command | Returns |
|---|---|---|
| "How does X work?" / "Where does Y live?" | `snapctx context "<query>"` | Top symbols + their full source + callees + callers + file outlines (one shot, ~3–10 k tokens). **Audit-aware**: phrasings like *"every place that uses X"* trigger an exhaustive `find` in parallel and attach the results. |
| "What's in this repo?" (orientation) | `snapctx map [--prefix src/] [--depth 1\|2]` | Repo-wide table of contents — every indexed file's top-level symbols grouped by directory. `--depth 2` adds class methods. **Always your first call in an unfamiliar repo.** |
| "Find a symbol by name or concept (ranked)" | `snapctx search "<query>" -k 10` | Top-K ranked symbols + signatures (no bodies). Add `--with-bodies` to inline; `--also <term2>` to batch keywords; `--kind function\|method\|class\|component\|interface\|type\|constant` to filter. |
| "What's in this file/dir?" | `snapctx outline <path>` | Symbol tree (heading tree for Markdown, key list for configs, structural tree for code). `--with-bodies` to inline. |
| "Show me this exact symbol's source" | `snapctx source <qname>` | Full body. `--with-neighbors` adds resolved callee signatures. |
| "Who calls X? What does X call?" | `snapctx expand <qname> --direction callees\|callers\|both --depth 1\|2` | Call-graph neighborhood. |
| "Every place that uses literal L (inside symbols)" | `snapctx find "<literal>"` | Exhaustive — no top-K cap. `--with-bodies` inlines containing symbols; `--with-callers` adds depth-1 caller lists per hit (audit + impact analysis in one call). |
| "Find raw text anywhere — comments, prose, configs, env files" | `snapctx grep "<pattern>" [--regex] [-i] [--in <path>] [-C N]` | Literal or regex over **every** gitignore-respected text file. Code-file hits are annotated with `qname` so you can pivot straight to `snapctx source`. |

### Write ops — qname-addressed, syntax-checked, atomic per file

You don't need to read a file before editing it. Every write op:
- accepts a **qname** (or path) as the address — no line-number bookkeeping;
- runs a **syntax pre-flight** before writing (Python `ast.parse`, TS/TSX tree-sitter) and refuses edits that would leave the file unparseable;
- is **per-file atomic** — if any change in a file fails the pre-flight, none of that file's changes land (other files succeed);
- guards against **stale coordinates** — refuses if the file's SHA has drifted since the last index, telling you to re-query.

CLI exposes `edit` and `insert`; the rest are Python-API-only and called via `from snapctx.api import …`. If you're driving snapctx from a non-Python agent, shell into a tiny `python -c "from snapctx.api import …"` helper.

| Task | Op | Notes |
|---|---|---|
| Replace one function / class / method body | `snapctx edit <qname> <body_file>` *or* `edit_symbol(qname, new_body)` | `new_body` is the COMPLETE replacement (def line through last statement). Works for Python and TS/TSX. |
| Insert a NEW top-level symbol next to an existing one | `snapctx insert <anchor_qname> <body_file> [--position before\|after]` *or* `insert_symbol(file, anchor_qname, position, body)` | Use to add a function / class / type alias / component without rewriting the file. |
| Cross-symbol consistency change in one shot | `edit_symbol_batch(edits=[{qname, new_body}, …])` | Per-file atomic. Use for "rename a parameter everywhere", "add tracing to N functions", etc. |
| Delete a function / class / method | `delete_symbol(qname)` | Trims surrounding blank lines so PEP-8 / Prettier spacing stays clean. |
| Add or remove an import | `add_import(file, statement)` / `remove_import(file, statement)` | Idempotent (no-op if already present / absent). For Python, docstring-aware: `add_import` lands AFTER a leading module docstring, not above it. |
| Create / delete a file | `create_file(path, content)` / `delete_file(path)` | `create_file` runs syntax pre-flight on parser-supported languages. `delete_file` refuses paths outside the root. |
| Move / rename a file | `move_file(src, dst)` | Returns `importing_files` — iterate over them and drive coordinated import-path rewrites with `add_import` / `remove_import` (or `edit_symbol`). |
| Rename a symbol everywhere | `rename_symbol(old_qname, new_name)` | Coordinated: the def + every caller body + every import line are rewritten in one op. Word-boundary substitution. Refuses on collision (target qname exists). Filtered by the def's module suffix so unrelated namesakes elsewhere are NOT touched. |

**Worked example — coordinated rename across def, callers, and imports:**

```python
from snapctx.api import rename_symbol
result = rename_symbol("pkg.core:calculate_total", "compute_total", root=".")

# {old_qname, new_qname, edits_applied, imports_updated, edits_skipped, files_touched}
```

One call replaces: grep for caller files → read each → edit each by hand → fix imports separately → re-run grep to confirm. ~6× fewer tokens; never partially applies.

### Pick the right tool — decision rules

1. **Always start with `snapctx_map`** in an unfamiliar repo, before anything else. One call shows you the whole layout.
2. For a question with a concept ("how does auth work", "where is the rate limiter") → `snapctx context`.
3. For a known symbol name → `snapctx search`, then `snapctx source <qname>` for the body.
4. For "what calls / is called by X" → `snapctx expand`.
5. For a literal you know is in code → `snapctx find` (exhaustive, scoped to symbol bodies).
6. For a literal that might live outside symbols (URLs, env names, TODO markers, README prose, config keys) → `snapctx grep`.
7. To **change** code → `snapctx edit` / `insert` / `delete_symbol` / `rename_symbol` / `edit_symbol_batch`. Don't read-then-edit unless you genuinely need the surrounding file context.

### When to fall back to the agent's built-in tools

- **`Grep`** — only for filename-pattern globs (e.g. "find every `*_test.py`"). Content search is covered by `snapctx_grep`.
- **`Read`** — only when you need a *whole file* end-to-end (rare). `snapctx_outline` shows structure; `snapctx_source <qname>` gives you any symbol; `snapctx_grep` returns matches with N lines of context.
- **`Glob`** — same as `Grep`: only for filename patterns. Use `snapctx_map` to see the directory tree.
- **`Edit`/`Write`** — only for non-code text snapctx doesn't parse (binary configs, lockfiles, generated artifacts).

### What just works

- **No setup**: first query auto-indexes; subsequent queries auto-refresh from changed file SHAs.
- **No `--root`**: snapctx walks up from CWD to find the nearest `.snapctx/index.db`.
- **Monorepos**: launching from a parent of indexed sub-projects fans out queries in parallel; each result is tagged with a `root` field (e.g. `"backend"` / `"frontend"`).
- **Vendor / generated noise filtered**: `node_modules`, `.venv`, minified bundles, `dist/`, gitignored paths — all skipped by default.
- **Third-party code on demand**: prefix a query with a package name (`"django: queryset filter"`) to route to that package's own isolated index. First use ingests the package; subsequent calls are zero-cost. `--pkg <name>` is the equivalent for ops without a free-text query (`source`, `outline`, `expand`).

### Output format

Every command returns JSON on stdout (pipe to `jq`). Stderr carries progress / refresh notes — they don't pollute the JSON.

### Tips

- `snapctx context` paraphrasing rule: if the response targets the wrong area, rephrase the query — hybrid mode adapts ranker weights to query style (identifiers vs concepts).
- `--mode lexical` skips the embedder (~50 ms saved cold) — use when the query is an exact identifier.
- For just signatures (no bodies), prefer `search` over `context` to save tokens.
- All write ops can be tested against a throwaway clone before they touch your live tree — clone the repo to `/tmp`, run them there, diff.
```

---

## Why bother (the numbers)

An agent investigating an unfamiliar codebase burns tokens the same way every time: grep for some terms, read 2–3 candidate files in full, grep again, read more files, synthesize. The cost compounds with question difficulty.

We measured both paths on three real codebases (a Django backend, zustand, and snapctx itself) — **9 cross-cutting questions** of the kind agents actually struggle with: *"audit every place that calls into an LLM provider"*, *"trace how a state update propagates from setState through equality checks to subscribers"*, *"list every CLI subcommand and which API function it dispatches to"*. snapctx ran via the in-process Python API (the path you get with `snapctx watch` or library use); grep+read used real `grep -ri` calls plus reading the top-K most-frequently-matching files (K = 3 for narrow, 6 for survey, 10 for audit; matching realistic agent behavior on each difficulty).

| Codebase | Difficulty | Question (abridged) | snapctx tokens | grep+read tokens | **ratio** | tool calls |
|---|---|---|---:|---:|---:|---:|
| Django backend | audit  | every Django model field + app | 7 k | 155 k | **22×** | 1 → 14 |
| Django backend | audit  | every LLM provider call + model + temperature | 10 k | **204 k** | **20×** | 1 → 17 |
| Django backend | survey | full flow API → views → DB for verse fetch | 14 k | 58 k | 4× | 1 → 11 |
| Django backend | survey | every DRF throttle / permission class + where | 7 k | 23 k | 3× | 1 → 11 |
| zustand | audit  | setState → equality → subscribers + persist | 6 k | 95 k | **17×** | 1 → 18 |
| zustand | survey | every middleware + state shape | 5 k | 77 k | **15×** | 1 → 11 |
| snapctx | audit  | every Index() open + connection lifecycle | 6 k | 40 k | 7× | 1 → 18 |
| snapctx | audit  | every CLI subcommand + API dispatch | 4 k | 44 k | **10×** | 1 → 17 |
| snapctx | survey | snapctx context → embedder load → SQLite search | 9 k | 41 k | 5× | 1 → 11 |

**The pattern.** Audit-class questions ("list every X and its Y") — where grep+read forces the agent to read 8–14 files in full — consistently hit **10–22× token reduction**. Survey questions ("trace this flow") land at **3–5×**. The harder the question, the wider the gap.

**Exhaustive literal audits — `find` vs ranked `search`.** Some audits are about *every* call site of a known string, not the most relevant ones. Ranked search caps the long tail; `find` walks the indexed bodies and returns every hit. Measured on a Django backend with a Sonnet sub-agent answering *"audit every `transaction.atomic` site"*:

| Approach | Sites found | snapctx calls | Bash calls | Tokens | Wall |
|---|---:|---:|---:|---:|---:|
| `search --mode lexical -k 100 --with-bodies` | 9 / 22 | 11 | 12 | 37 k | 185 s |
| `context "audit every transaction.atomic site"` (audit-aware) | 22 / 22 | 2 | 3 | 36 k | 81 s |
| **`find "transaction.atomic" --with-bodies`** | **22 / 22** | **1** | **1** | **33 k** | **61 s** |
| `find "..." --with-bodies --with-callers` | 22 / 22 | 1 | 2 | 35 k | 67 s |
| Reference: `grep -rn` + read top files | 22 / 22 | n/a | 50 | 39 k | 146 s |

`find` matches `grep` exactly (22 / 22) in a single tool call. The ranked-search variant misses ~60% because the agent stops after a few overlapping `-k 100` pages. **For audit-class questions, the bottleneck wasn't ranking quality — it was the contract: ranked + capped vs exhaustive.**

**`find` vs `grep -rn` — the measured gap:**

- **50× fewer tool calls** (1 vs 50). Every grep iteration forces the agent to read raw lines, infer which file to open next, issue a read, and decide whether to grep again. `find` does all of that in the index.
- **2.4× faster wall clock** (61 s vs 146 s).
- **Output tokens roughly equal** (33 k vs 39 k) — the token savings vs `grep` aren't the story here. The story is the call-count collapse and what the output *is*.

What `grep` returns that the agent still has to process: raw matched lines with no surrounding context, no function name, no caller information. What `find` returns: `qname` (the enclosing function or method, resolved), file, line, and the matching line — structured, ready to synthesize. Add `--with-bodies` and you get the full enclosing symbol; add `--with-callers` and you get impact analysis in the same call. There is no single `grep` command that does any of those.

The audit-aware `context` row is the win for agents that default to `context` as their first move: same coverage as raw `find` (22 / 22), at the cost of one extra targeted call to fetch bodies. They no longer need to know whether to reach for `context` or `find` — `context` routes them. The `--with-callers` row trades ~6 s wall and ~2 k tokens for inline impact analysis; in our run it produced a sharper synthesis that explicitly addressed orphaned atomic blocks.

**Latency for snapctx itself:**

- **Warm path** (in-process, with `snapctx watch` or library use, indexed): **6–16 ms per query** — the embedder model loads once and stays resident.
- **Cold path** (one-shot CLI invocation, indexed): **~0.3–1.1 s** (most of that is the ~250 ms fastembed model load each run).
- **First query in a fresh repo** (auto-indexes): ~5–10 s for a few hundred files. After that, every refresh is SHA-skip and finishes in <200 ms.

The cold-CLI ~1 s vs warm 6 ms isn't the win that matters at the agent level; the round-trip-count collapse is. But if your agent uses snapctx through `snapctx watch` or imports the Python API, every query lands under 20 ms.

---

## Monorepos

If you've indexed several sub-projects under one parent (typical: a Python `backend/` and a Next.js `frontend/`), launch `snapctx` from the parent and queries fan out across all of them:

```bash
snapctx index ./backend
snapctx index ./frontend

# From the parent — no .snapctx of its own — queries hit both:
snapctx context "session login flow"
```

Each result is tagged with a `root` field (`"backend"` / `"frontend"`). `expand` and `source` route to whichever root holds the qname; `outline` routes by file path prefix.

`snapctx roots` shows which indexes would be queried from your current directory.

---

## Querying third-party packages

A query can target a third-party package by **prefixing the package name**. No prefix → the repo's own code, full stop. Prefixed → snapctx ensures that one package is indexed (one-time, on demand) and runs the query against the package's *own* isolated index.

```bash
# Repo only — no vendor packages searched.
snapctx context "session login"

# Routes to django's per-package index. First call ingests just django
# (one-time ~50 s for ~900 files). Subsequent calls are zero-cost.
snapctx context "django: queryset filter chain lazy"
# → { "scope": "django", "seeds": [ { "qname": "db.models.query:QuerySet._chain", … } ] }
```

**Why per-package isolation rather than merging into the repo's index?** Two reasons. (1) Vector-search neighborhoods are sharper over a single coherent corpus — searching for `QuerySet` inside the django scope can't be polluted by your own filter classes. (2) Qnames inside the package's index are re-rooted at the package, so they look like Django's actual module structure (`db.models.query:QuerySet`) instead of a long `.venv.lib.pythonX.Y.site-packages.django.…` prefix.

**Storage layout:**

```
<root>/.snapctx/
  index.db                  ← repo (default — what queries hit with no prefix)
  vendor/
    django/index.db         ← one isolated index per indexed package
    react/index.db
```

**Routing rules** for the `<pkg>:` prefix:
- The head must be a single identifier (letters, digits, underscore, hyphen) — `django.db.models:QuerySet` stays a qname, not a prefix.
- The head must match a directory under `<root>/{.venv,venv,env}/lib/python*/site-packages/<name>/` (Python) or `<root>/node_modules/<name>/` (Node, top-level only). An already-indexed package also matches even if its source dir was later deleted.
- No match → no routing, the colon is treated as part of the query.

**Manual control** for ops that don't take a free-text query:

```bash
snapctx source --pkg django "db.models.query:QuerySet"  # equivalent of "django: …"
snapctx vendor list                                      # see indexed + available
snapctx vendor forget django                             # drop a package's index
```

`--pkg` is available on `search`, `context`, `expand`, `outline`, and `source`. Vendor scoping is single-root only — run from inside the specific sub-project that owns the venv.

**Subsequent scoped calls are fast.** A repo query auto-refreshes the repo's index (SHA-skip ~750 ms on a 300-file project). A scoped query *skips* the repo refresh entirely. End-to-end latency: **~350 ms warm**, dominated by fastembed model load.

### Cross-package call graph (lazy stitching)

When `expand` or `context` traverses a call inside one indexed package and the callee was imported from *another also-indexed* package, the resolver follows the file's `imports` table to the right sibling index and returns the resolved symbol with a `package` tag.

Example: inside django, `tasks.base:Task.call` invokes `async_to_sync`. If you've also indexed `asgiref:`, `expand` returns:

```jsonc
{ "qname": "sync:async_to_sync", "package": "asgiref", … }
```

instead of an unresolved name. Honors the explicit-prefix rule: cross-resolution *only* peeks into packages you've already chosen to index — it never spontaneously fans out into something you didn't ask for. Calls into unindexed packages stay marked `resolved: false`, and you can `--pkg <name>` to bring them in.

---

## Per-repo config (optional)

Drop a `snapctx.toml` at the repo root to override walker defaults — extra skip directories, vendor-bundle filter, file-size cap, language enable list, glob include/exclude. Without a config file, behavior is unchanged.

```toml
# snapctx.toml — every key optional. Defaults match the no-config behavior.

[walker]
# Add to the always-skip directory list (joined with .git, .venv,
# node_modules, vendor, dist, build, ...).
extra_skip_dirs = ["legacy", "third_party"]

# Add filename suffixes to the vendor-bundle skip list (joined with
# .min.js, .bundle.js, *-bundle.js, *.standalone.js, .map, ...).
extra_skip_suffixes = [".generated.ts"]

# Force-include paths even when .gitignore would skip them.
extra_include = ["vendor/internal-fork/**"]

# Force-exclude regardless of .gitignore.
extra_exclude = ["docs/generated/**", "**/*.snapshot.tsx"]

# Toggles (defaults match current behavior).
skip_vendor_bundles  = true      # filter .min.js / *-bundle.js / .map / ...
skip_vendor_packages = true      # filter node_modules / .venv / vendor / ...
respect_gitignore    = true      # honor .gitignore
max_file_size        = 256000    # bytes; default 250 KiB

# Restrict to specific parsers (default: every parser is active).
# Valid values: "python", "typescript", "shell".
languages = ["python", "typescript", "shell"]
```

Unknown keys are tolerated (forward-compat); type errors on known keys raise with the file path so they're easy to fix.

---

## Keep the index hot: `snapctx watch`

```bash
snapctx watch
```

Sits on the repo, debounces filesystem events, re-indexes on save (typically <200 ms per delta because of SHA-skip). Inside the watch process the embedder stays loaded, so query latency drops to single-digit ms.

---

## How it works

### Indexing (one-time per repo, re-runs incrementally)

`snapctx index <root>` walks the repo respecting `.gitignore` and per-extension parsers, then builds three artifacts in `<root>/.snapctx/`:

1. **`symbols`** — every function, method, class, nested closure, interface, type alias, React component, module (with file-level docstring or leading JSDoc), and module-level / class-level constant. Fields: qualified name (`module.path:Class.method`), kind, signature, docstring, file, line range, decorators, base classes, source SHA.
2. **`calls`** — caller → callee edges. Names are heuristically resolved against the caller's import table, with optimistic resolution through base-class MRO for `self.X` method calls. Calls in decorator arguments and default values are attributed to the module, not the decorated function. After full ingest, two post-passes fix up edges:
   - **Demote** — null any callee qname that didn't land on a real symbol.
   - **Promote** — resolve forward-referenced `self.X()` calls where the target was defined later in the same class body.
3. **`symbols_fts`** (SQLite FTS5) + **`symbol_vectors`** (384-dim `bge-small-en-v1.5` embeddings) — for hybrid search.

Incremental: files whose SHA matches the stored value are skipped. Only changed files get re-parsed and re-embedded.

The walker skips vendored/bundled assets by default: `node_modules`, `.venv`, `dist`, `vendor`, `bower_components`, `*.min.js`, `*.bundle.js`, `*-bundle.js`, `*.standalone.js`, `*.lib.js`, `*.worker.js`, `*.map`, plus any source file over 250 KB.

### Query-time

All operations read from the same SQLite file. They're cheap and composable; `context` is the all-in-one wrapper.

- **`search_code(query, k, kind?, mode, with_bodies?, also?)`** — three modes: `lexical` (FTS5/BM25, ~2 ms), `vector` (cosine over embeddings, ~5 ms), `hybrid` (weighted RRF of both, default). Hybrid uses `vec_weight=1.5`, `lex_weight=1.0`, plus a `test_penalty=0.6` multiplier so test methods don't out-rank real code. `with_bodies=True` inlines source bodies (with constants pre-resolved) for one-shot audits; `also=[…]` runs the same query for several related terms in a single call.
- **`find_literal(literal, kind?, in_path?, with_bodies?, with_callers?)`** — exhaustive literal-substring scan over indexed symbol bodies. Returns every match (file, qname, match_line, match_text), innermost-symbol deduped so a method beats its enclosing class. Complement to `search_code` when the question is "every place that uses X" rather than "the most relevant place". `with_callers=True` attaches the deduped depth-1 caller list to each hit so audit + impact analysis fits in one call.
- **`expand(qname, direction, depth)`** — walk the call graph. Returns signatures + docstring summaries of neighbors, no bodies.
- **`outline(path, with_bodies?)`** — file or directory symbol tree, nested by containment; `with_bodies=True` inlines source for every symbol.
- **`map_repo(prefix?, depth?)`** — repo-wide table of contents. Returns every indexed file's top-level symbols (qname, signature, 1-line docstring, line range, decorators), grouped by directory, with each file's module docstring hoisted to a file-level `summary`. `depth=2` adds direct children (class methods, nested functions). `prefix` scopes to a sub-tree. Query-free orientation tool — complement to `context`/`search`, not a replacement.
- **`get_source(qname, with_neighbors)`** — full source of a single symbol; `with_neighbors=True` appends signatures of resolved callees.
- **`context(query, …)`** — the one-shot. Runs `search_code` (or fast-paths to a direct qname match when the query contains `:` and matches a known qname). For each of the top `k_seeds=5` hits:
  - Signature, docstring, file, line range, decorators, score.
  - **Depth-2 call trace** — up to `neighbor_limit=8` direct callees, each resolved callee carrying up to `max(3, neighbor_limit//2)` of its own callees. Same shape for callers. An agent sees the full flow (e.g. `Runner.emit → _publish_delta → client.publish`) without a follow-up `expand` call. Unresolved calls to Python builtins (`print`, `len`, `isinstance`, …) are filtered so the graph stays focused on domain code.
  - Full source body (capped at `body_char_cap=2000` chars each) for the top `source_for_top=5` seeds.
  - **Constant-alias resolution**: if the seed is `NAME = OTHER_NAME`, the chain is followed (up to 3 hops, cross-file) and the terminal literal is attached as `resolved_value`. So the agent sees the real string (`'claude-opus-4-5'`) without a separate `source` call on the registry module.

  File outlines for up to 8 unique files among the search candidates — the candidate pool is overfetched beyond the top-K seeds (default `outline_discovery_k=15`) so survey questions get full coverage. Typical output: 3–8 k tokens.

  **Audit-aware enrichment.** A conservative classifier (`extract_audit_literal`) detects audit phrasings that wrap a single literal — *"audit every X"*, *"every place that uses X"*, *"list every X"* — and returns the literal only when exactly one identifier-shaped token survives stripping audit fillers ("site", "call", "place", "uses", …). On a hit, `context` runs `find_literal` in the same call and attaches a `find_results` block (file, qname, match_line, match_text per site, no bodies — agent re-issues `find <lit> --with-bodies` if needed). Multi-literal questions ("every LLM provider call") and concept questions ("every model field") deliberately skip the find block to avoid clutter.

### Why hybrid won

We raced lexical / vector / hybrid on real codebases:

**Q1 — focused** (*"what fields does `TranslationVerse` have?"*):

| Mode | Calls | Tokens | Duration | Accurate? |
|---|---:|---:|---:|:---:|
| lexical | 4 | 23 k | 29 s | ✓ |
| hybrid | 4 | 21 k | 21 s | ✓ |
| **`context()`** | **1** | 24 k | **17 s** | **✓** |

**Q5 — survey** (*"list every LLM provider + model + phase"*):

| Mode | Tool calls | Agent tokens | Duration | Notes |
|---|---:|---:|---:|---|
| lexical (no constants) | 28 | 80 k | 112 s | missed `DEFAULT_*_MODEL` constants |
| lexical + constants | 16 | 54 k | 63 s | complete |
| vector-only | 6 | 44 k | 38 s | complete, some tail noise |
| composable hybrid | 15 | 41 k | 57 s | most thorough |
| **`context()`** | **1** | 42 k | **30 s** | **complete with alias resolution** |

The win for `context()` comes from collapsing 15 reasoning-and-call round-trips into 1, not from making a single call faster.

The RRF math is just `rrf(q) = Σ weight / (60 + rank)` — sum the weighted reciprocal-rank contributions from each ranker. The 60 is the standard RRF constant; the only tunable is the weight ratio.

**Why RRF, not score fusion?** FTS5 BM25 and cosine similarity live in different units; normalizing is lossy and config-sensitive. RRF uses ranks alone — robust, and the only knob (the weight ratio) was tuned once on real queries.

### Performance

Measured on a mixed-language monorepo (Python Django backend + Next.js frontend, 754 files, 3,290 symbols), M-series Mac:

| Path | Latency |
|---|---:|
| Cold index from scratch (parse + embed all symbols) | ~10 s |
| Re-index after one-file edit (SHA-skip the rest) | <200 ms |
| Cold CLI, hybrid `context()` call | ~380 ms |
| Cold CLI, exact qname (fast path) | ~30 ms |
| Warm in-process, hybrid `context()` (depth-2 trace) | **5–10 ms** |
| Warm in-process, exact qname | **1–2 ms** |

The cold-to-warm delta is almost entirely the fastembed ONNX model load. Inside `snapctx watch` (or the internal serve daemon — `python -m snapctx._serve`, used by the warm client) the model loads once and every query is single-digit ms. A first-class `snapctx serve` CLI command is on the roadmap.

The three levers we tuned (85 s → 10 s on the same repo): walker-level vendor-bundle filter, TS signature truncation to 240 chars so massive `const X: ColumnDef<T>[] = [...]` declarations don't bloat the index, and `fastembed` batch size = 4 (counter-intuitive, but smaller batches mean less ONNX padding waste on mixed-length texts).

---

## Use it as a Python library

Everything the CLI exposes is also a Python function. Import from `snapctx.api`:

```python
from snapctx.api import (
    context, search_code, find_literal, expand, outline, map_repo,
    get_source, index_root,
    # write ops:
    edit_symbol, insert_symbol, edit_symbol_batch, delete_symbol,
    add_import, remove_import,
    create_file, delete_file, move_file,
)

# Build or refresh the index.
index_root("/path/to/repo")

# One-shot context pack.
pack = context("how does session authentication work", root="/path/to/repo")
for seed in pack["seeds"]:
    print(seed["qname"], seed["signature"])
    if "resolved_value" in seed:
        print("  → ", seed["resolved_value"]["value"])

# Exhaustive literal-substring audit.
result = find_literal("transaction.atomic", root="/path/to/repo", with_bodies=True, with_callers=True)
for match in result["matches"]:
    print(match["qname"], match["file"], match["match_line"])
    for caller in match.get("callers", []):
        print("  ←", caller["qname"])

# Composable operations.
hits = search_code("throttle", k=3, mode="vector", root="/path/to/repo")
neighbors = expand("auth.service:login", direction="callers", root="/path/to/repo")
tree = outline("src/auth/service.py", root="/path/to/repo")
src = get_source("auth.service:login", with_neighbors=True, root="/path/to/repo")
```

All functions return JSON-serializable dicts.

### Write operations

The same qname-based addressing also drives writes. `edit_symbol` replaces a single symbol's body, `insert_symbol` adds a new one next to an anchor, `edit_symbol_batch` applies many edits in one call:

```python
from snapctx.api import edit_symbol, insert_symbol, edit_symbol_batch

# Replace one symbol's body. Splices in at the indexed line range,
# checks the file's SHA hasn't drifted since indexing, runs a syntax
# pre-flight (ast.parse for Python, tree-sitter for TS/TSX), and
# re-indexes the file before returning.
edit_symbol(
    "auth.service:login",
    "def login(username, password):\n    return _verify(username, password)\n",
    root="/path/to/repo",
)

# Add a brand-new top-level function next to an anchor.
insert_symbol(
    "auth.service:login",
    "\n\ndef logout(session_id):\n    return _drop(session_id)\n",
    root="/path/to/repo",
    position="after",
)

# Apply many edits in one call. Per-file atomic: a syntax error on
# any edit in file X rolls back X's edits; files Y, Z still land.
# One re-index for the whole batch.
edit_symbol_batch(
    [
        {"qname": "auth.service:login",  "new_body": "..."},
        {"qname": "auth.service:logout", "new_body": "..."},
        {"qname": "auth.tokens:revoke",  "new_body": "..."},
    ],
    root="/path/to/repo",
)
```

All write ops return structured `{"error": ..., "hint": ...}` dicts on failure (`not_found`, `stale_coordinates`, `syntax_error`, `write_failed`, …) so an LLM agent can treat them as recoverable and retry. Vendor scopes are read-only — write ops refuse them.

For the cases that don't fit the symbol model (imports, file lifecycle), there are dedicated ops:

```python
from snapctx.api import (
    delete_symbol,
    add_import, remove_import,
    create_file, delete_file, move_file,
)

# Drop a symbol entirely (vs edit_symbol(q, "") which corrupts spacing).
delete_symbol("auth.legacy:old_login", root="/path/to/repo")

# Add or remove an import. Idempotent: re-running with the same
# statement is a no-op. Lands at the bottom of the existing import
# block, or at the top of the file if there are no imports yet.
add_import("auth/service.py", "from .tokens import revoke", root="/path/to/repo")
remove_import("auth/service.py", "import legacy_auth", root="/path/to/repo")

# File-level lifecycle.
create_file("auth/tokens.py", "def revoke(s):\n    ...\n", root="/path/to/repo")
delete_file("auth/legacy.py", root="/path/to/repo")

# Move + identify import sites that need rewriting.
result = move_file("auth/legacy.py", "auth/legacy_v0.py", root="/path/to/repo")
for f in result["importing_files"]:
    remove_import(f, "from auth.legacy import x", root="/path/to/repo")
    add_import(f, "from auth.legacy_v0 import x", root="/path/to/repo")
```

For monorepos, the multi-root variants (`context_multi`, `search_code_multi`, `expand_multi`, `outline_multi`, `get_source_multi`) accept a list of roots and merge / route across them. The CLI uses these automatically when `discover_roots()` returns more than one root.

```python
from snapctx.api import context_multi
from snapctx.roots import discover_roots
from pathlib import Path

roots = discover_roots(".")            # walks up first; falls back to one-level walk-down
pack = context_multi("login session", roots, anchor=Path("."))
# Each seed has a "root" field tagging which sub-project it came from.
```

---

## Response shapes

### `context` (abridged)

```jsonc
{
  "query": "…",
  "mode": "hybrid",           // or "lexical" | "vector" | "exact"
  "scope": "django",          // present only for vendor-prefix queries
  "seeds": [
    {
      "rank": 1,
      "qname": "module.path:ClassName.method",
      "kind": "method",        // function | method | class | module | constant | …
      "signature": "def method(self, arg: T) -> R",
      "docstring": "One-line summary.",
      "file": "/abs/path/file.py",
      "lines": "42-67",
      "score": 0.0381,
      "decorators": ["@property"],
      "callees": [
        {
          "qname": "other:helper",
          "signature": "def helper(x)",
          "docstring": "…",
          "line": 55,
          "package": "asgiref",            // present only for cross-package edges
          "callees": [                     // depth-2 nested hop
            { "qname": "util:sanitize", "signature": "def sanitize(x)", "line": 12 }
          ]
        }
      ],
      "callers": [ /* same shape, with nested "callers" at depth 2 */ ],
      "source": "def method(self, arg: T) -> R:\n    …",
      "resolved_value": {                  // present only for constant-alias seeds
        "chain": ["defaults:DEFAULT_MODEL"],
        "terminal_qname": "defaults:DEFAULT_MODEL",
        "value": "'claude-opus-4-5'"
      }
    }
  ],
  "file_outlines": [
    {
      "file": "/abs/path/file.py",
      "symbols": [ { "qname": "…", "kind": "…", "signature": "…", "lines": "10-30" } ]
    }
  ],
  "token_estimate": 3046,
  "hint": "If it's still not enough, call expand/outline/source on a specific qname."
}
```

### `search_code`

```jsonc
{
  "query": "…",
  "mode": "hybrid",
  "results": [
    {
      "qname": "…",
      "kind": "…",
      "signature": "…",
      "docstring": "…",
      "file": "…",
      "lines": "…",
      "score": 0.037,
      "next_action": "expand"    // expand | outline | read_body | enough
    }
  ],
  "hint": "Call expand('<qname>') to see what this depends on."
}
```

`next_action` is the tool's opinion about what the agent should do next with the top hit. Classes → `outline`. Functions with short docstrings → `read_body`. Otherwise → `expand`.

### `find_literal`

```jsonc
{
  "literal": "transaction.atomic",
  "match_count": 22,
  "truncated": false,
  "matches": [
    {
      "qname": "parser.services:StrongsComparisonService.save_comparison",
      "kind": "method",
      "signature": "def save_comparison(self, …)",
      "file": "/abs/path/parser/services.py",
      "lines": "1040-1080",
      "match_line": 1056,
      "match_text": "        with transaction.atomic():",
      "source": "def save_comparison(…):\n    …",   // present with --with-bodies
      "callers": [                                   // present with --with-callers
        { "qname": "parser.views:ComparisonView.post", "line": 214 }
      ]
    }
  ],
  "hint": "22 sites found, bodies inlined. Callers attached."
}
```

---

## Security model

snapctx is a local read-mostly CLI. Worth understanding what it touches.

**What it reads:**
- Files under the discovered `.snapctx` root, and only files the walker indexed:
  - Python (`.py`, `.pyi`), TypeScript (`.ts`, `.tsx`, `.js`, `.jsx`), shell (`.sh`, `.bash`) — **not** `.env`, credentials, logs, binaries.
  - Respects `.gitignore` — anything excluded there is invisible.
  - Skips `.git`, `.venv`, `node_modules`, `__pycache__`, `.snapctx`, `dist`, `build`, `.tox` by default.
  - On-demand vendor indexing reads from `.venv/lib/python*/site-packages/<name>/` and `node_modules/<name>/` *only* when a query is prefixed with `<name>:` or `--pkg <name>` is passed. Stored separately under `.snapctx/vendor/<name>/`.

**What it writes:** Exactly one place: `<root>/.snapctx/index.db` (+ WAL/SHM sidecars). Nothing else on disk is ever touched.

**Network:** None at runtime. The ONNX embedding model is downloaded once on first `snapctx index` (into `~/.cache/huggingface/`), then every future query runs fully offline.

**Subprocess / code execution:** None. snapctx doesn't shell out, doesn't `eval`, doesn't spawn Python subprocesses. It parses with stdlib `ast` + tree-sitter, runs SQLite queries, and does ONNX inference.

**Path-traversal protection:** `outline(path=…)` only returns symbols that exist in the index. A request for `/etc/passwd` or any other off-root path returns an empty result; no filesystem read happens. `source <qname>` reads the file path stored on the matched symbol row, which the walker guarantees is under the indexed root.

**The one real caveat — same as `Read` / `Grep`:** Any secret hardcoded into a source file (API keys in module constants, credentials baked into source) becomes discoverable via the semantic search. `Read` and `Grep` already expose that; snapctx makes it *more* findable. Don't commit secrets.

**Summary:** exposure = same set of files your agent could already `Read`/`Grep`, minus all non-source content, minus anything in `.gitignore`. No network, no exec, no writes outside `.snapctx/`.

---

## What's indexed, what's not

**Indexed:**
- **Python:** functions and methods (`def`, `async def`, including nested / closure definitions); classes (with base-class list for MRO-aware `self.X` resolution); module-level constants (`UPPER_CASE = literal | identifier | collection`); class-level constants; imports.
- **TypeScript / TSX / JSX:** functions and arrow-const functions; classes (with `extends` / `implements` base chain); interfaces and type aliases; enums; React components (capitalized name + JSX in body); module-scope typed constants. JSX usage is tracked as a call edge (`<Button />` → `Button:Button`).
- **Shell (`.sh`, `.bash`):** module symbol per script (with leading-comment block as docstring); function definitions in both POSIX (`name() { … }`) and ksh (`function name { … }`) form; `source` / `.` directives as imports; intra-script function calls as call edges. External binaries (`aws`, `docker`, `git`) are intentionally skipped.
- **Module docstrings** — Python files with a top string docstring and TS/JSX files opening with a `/** … */` block. This captures the architectural "why" of a file, which often isn't repeated on any single class or function.
- **Call edges**, with optimistic `self.X` resolution through base classes, plus a post-ingest *promote* pass for forward references.

**Not indexed (by design):**
- Strings inside source files (so `urlpatterns = [...]` route strings are not searchable).
- Comment blocks (except file-leading JSDoc).
- Runtime-dynamic symbols (metaclasses, `type(...)` factories, monkeypatched attributes).
- Bundled / vendored / minified JS (walker skips files over 250 KB and common bundle suffixes like `*-bundle.js`, `*.lib.js`, `*.standalone.js`, `*.worker.js`, `*.map`).
- Calls that run at module-load time, not runtime — decorator arguments, default values, and type annotations aren't attributed as the enclosing function's callees.

**Known rough edges:**
- `self.x.y.z` attribute chains are left unresolved by design — guessing is worse than saying "don't chase this".
- Django ORM-style `Model.objects.filter(...)` is demoted to unresolved after the post-ingest sweep (the chain doesn't point at real symbols).
- Class hierarchies where the base lives behind a runtime import or dynamic `__init_subclass__` will miss MRO edges.
- TypeScript callee resolution is limited without a full type system: calls on parameters, local variables, imported constants, and `this.*` are often unresolved. Depth-2 traces are most useful on Python code paths.
- Shell heredocs (`<<EOF … EOF`) aren't tracked when matching braces; a heredoc body containing an unbalanced `{` could confuse function-end detection. Rare in practice.

---

## Running the test suite

```bash
# from the repo root
uv pip install --group dev      # or: uv pip install pytest
pytest
```

205 tests pass. First run downloads the ONNX embedding model (~30 MB, cached under `~/.cache/huggingface/`).

---

## Status and roadmap

**Shipped — validated on a mixed-language monorepo (Django + Next.js, 754 files / 3,290 symbols):**
- [x] Python AST extraction — functions, methods, classes, nested scopes, module-level + class-level constants, **module docstrings**
- [x] TypeScript / TSX / JSX parser (tree-sitter) — functions, arrow consts, classes, interfaces, type aliases, enums, React components, constants, imports, JSX usage as call edges, **leading JSDoc module docs**
- [x] Shell (`.sh`, `.bash`) parser — module symbol with leading-comment docstring, POSIX + ksh function forms, `source`/`.` imports, intra-script call edges
- [x] Call graph with MRO-aware `self.X` resolution, plus post-ingest **promote** pass for forward references
- [x] Decorator-arg / default-value / type-annotation calls filtered out of runtime call graph
- [x] SQLite + FTS5 lexical search
- [x] `bge-small-en-v1.5` embeddings + cosine vector search
- [x] Weighted RRF hybrid ranker with test-file demotion
- [x] One-shot `context()` — **depth-2 call-path trace**, constant-alias resolution, multi-file outlines from an overfetched candidate pool
- [x] Builtin-noise filter for unresolved callees (`?:print`, `?:len`, `?:isinstance`, …)
- [x] Fast path for exact-qname queries (~1 ms warm)
- [x] Incremental indexing (SHA-based)
- [x] Walker vendor-bundle / size filter — skips minified JS, source maps, `*-bundle.js`, `*.lib.js`, `*.standalone.js`, and anything over 250 KB
- [x] **Auto-discovery** — walks up from CWD to find the nearest `.snapctx/index.db`; falls back to one-level walk-down for monorepo parents with multiple indexed sub-projects
- [x] **Auto-indexing on first query** — if no index is reachable, queries build one transparently before answering
- [x] **Per-repo config** — optional `snapctx.toml` at the root overrides walker defaults; no config means no behavior change
- [x] **Multi-root fan-out** — queries from a parent dir hit every indexed sub-project in parallel and tag results with their `root` label
- [x] **File watcher** (`snapctx watch`) — debounced auto re-index on save, typical run ~5 ms warm
- [x] **On-demand vendor packages with per-package isolation** — prefix a query with `<pkg>:` (or pass `--pkg <name>`) and snapctx ingests just that package into its own dedicated index. Vector neighborhoods stay focused; qnames re-root at the package. Managed via `snapctx vendor list` / `vendor forget`
- [x] **Cross-package call-graph stitching** — when a call inside one indexed package targets a name imported from another *also-indexed* package, `expand` and `context` follow the import to the sibling index and return the resolved symbol tagged with its package
- [x] **`find` — exhaustive literal-substring enumeration** over every indexed symbol body. Closes the audit-class gap vs `grep`: returns every match (not ranked, not capped), with the containing qname attached. Validated 22 / 22 vs raw `grep` on a Django audit.
- [x] **`find --with-callers`** — attaches deduped depth-1 callers to each hit so audit + impact analysis ("every X site AND who triggers them") is one call
- [x] **Audit-aware `context`** — when the query is unambiguous audit phrasing wrapping a single literal, `context` runs `find` on the literal in the same call and attaches a `find_results` block. Makes "context first" the right move even for cross-cutting audit questions
- [x] **`search --with-bodies`** for one-shot audit-class queries (inlines source with constant pre-resolution); **`search --also <term2> [...]`** to batch related terms in one call
- [x] **`outline --with-bodies`** + directory mode — exhaustive enumeration over a folder
- [x] **Smart hints** in API responses — audit-class queries get a hint nudging the agent toward `--with-bodies` or `find`

**Planned next (snapctx core):**
- [ ] **`snapctx serve` daemon** — long-running process holds the fastembed model + SQLite handle warm; CLI invocations talk to it over a Unix socket. Closes the ~400 ms cold-CLI gap so every query is 5–10 ms whether or not you have `snapctx watch` running. Lifecycle: auto-start on first query, idle-stop after N minutes, single-instance lock per repo.
- [ ] **Lazy embedder loading** — quick win that lands today's cold-CLI cost at ~50 ms for `outline`, `source`, `expand`, and any `--mode lexical` query. Doesn't help hybrid `context`, but eliminates the model load for paths that don't need it.
- [ ] **TS scope tracker** — parameter / local / import resolution so TS callee traces aren't stuck at depth 1.

**Companion projects (snaptoolkit, planned):**
- [ ] **`snapdocs`** — same idea as snapctx, applied to documentation. Index a project's docs (Markdown, MDX, RST, in-tree ADRs, even fetched third-party docs) into FTS5 + embeddings; expose a `snapdocs context "<question>"` that returns the most relevant doc passages, anchored at heading level, with the section above and below for grounding.
- [ ] **`snappatch`** — symbol-level structured editing. Where snapctx *finds* the affected symbol and its dependencies, snappatch *edits* exactly that scope and nothing else. An agent calls `snappatch edit <qname> --instruction "..."` (or feeds a unified-symbol diff); snappatch loads the symbol body + the depth-1 callers/callees that constrain the change, applies the edit, and writes back **only the affected ranges** — no whole-file rewrites.

---

## License

MIT.
