# HuggingFace Tool — Design Spec (Parkour MCP)

**Status:** design proposal for implementation review
**Grounded in:** `parkour_mcp/github.py` (template), `docs/frontmatter-standard.md` (envelope
rules), `parkour_mcp/fetch_direct.py` (interception), and **live probing of the HF Hub API**
(every field, latency, and detector below was verified against real repos — see §13/§14).

**Reading order for implementers:** §2–§6 define the tool surface (mirrors the GitHub tool);
§7 is the dead-end rationale; §13 has the measured latencies and the authoritative `expand`
token list; **§14 is the quant-quality detection design, and §14.1a–§14.1c carry four mandatory
correctness preconditions plus one presence-vs-truthiness rule. Read those before writing any
detector.** Each precondition has a verified public repo that fails only that gate; skipping any
one of them makes the tool accuse an honest release of bloat. §11 records decisions already
resolved by the probing.

> Provenance note: this proposal came out of a multi-session quant-evaluation project against
> the Hub (DeepSeek-V4-Flash, Kimi-K2, MiMo-V2.5 families). Every dead-end in §7 and every
> detector in §14 is a real tool call that project wasted or a real trap it hit.

---

## 1. Motivation

The Hugging Face Hub has an unusually **predictable "what next" graph**. Landing on a model
repo, an agent almost always needs some subset of: the config (arch / `model_type` / quant),
the parameter count and size, whether it's gated, its `base_model` lineage, the file list
(sharded or not), and — for quant work — the *sibling quants in the same family*. During this
project's quant-evaluation work, virtually every model investigation was the same 3–6 fetches:
`config.json` → `model.safetensors.index.json` → `?blobs=true` for sizes/shas → a base-model
config → range-read a safetensors header. That's a textbook **beta** opportunity: one API call
plus a well-built frontmatter envelope can pre-answer nearly all of it.

The GitHub tool is the natural template: same shape of use cases (repo metadata, file reads,
directory listings, URL interception), same auth/token model, same fast-path-with-`see_also`
interception in `fetch_direct.py`.

**v1 scope:** models only (decided — rationale and the datasets/Spaces disposition in §13).

## 2. Patterns reused from the existing codebase

| Concern | GitHub tool | HuggingFace tool |
|---|---|---|
| URL classification | `_detect_github_url` → `GitHubUrlMatch(kind=…)` | `_detect_hf_url` → `HFUrlMatch(kind=…)` |
| WebFetch interception | `_github_fast_path` (in `fetch_direct.py`) | `_hf_fast_path` |
| Token / headers | `_get_github_token`, `_github_headers` | `_get_hf_token`, `_hf_headers` (Bearer HF_TOKEN, optional) |
| API request wrapper | `_github_request` (base `api.github.com`) | `_hf_request` (base `huggingface.co/api`) |
| Frontmatter base | `_fm_base(source, api="GitHub")` | `_fm_base(source, api="HuggingFace")` |
| Rate-limit warning | `_rate_limit_warning()` | `_hf_rate_limit_warning()` — HF sends RFC 9651 structured fields (`RateLimit` / `RateLimit-Policy`), **not** `X-RateLimit-*`; `github.py#_GitHubRateLimit` cannot be copied wholesale (§13) |
| Action dispatch | `github(action, query, …)` | `huggingface(action, query, …)` |
| Detection stays stateless? | GitHub detect consults token → lives in `github.py` | **Opposite: HF detect is pure → lives in `detection.py`** (see below) |

**Frontmatter-standard compliance (hard requirements):**
- `_build_frontmatter()` is the sole `---` producer; formatters emit body only.
- Error strings carry no frontmatter.
- Protected multi-contributor keys (`hint`, `warning`, `note`, `see_also`, `alert`) via
  `fm_entries.append(...)`, never assignment.
- **Trust boundary (critical for HF):** frontmatter carries only *structured / numeric / enum*
  fields and tool-authored hints. Anything free-text and uploader-controlled — the model-card
  prose, freeform tag strings, `cardData` descriptions — goes in the **fenced body**, never in
  frontmatter (frontmatter-injection vector). `base_model` and `model_type` are constrained
  (repo-path / registry enum) and may be factual fields *after shape validation*.

**`_detect_hf_url` belongs in `detection.py`, not `huggingface.py`.** The GitHub tool is the
exception here, not the precedent to copy. `github.py#_detect_github_url` consults the token for
one narrow, documented reason: *discussion* URLs return `None` when no token is configured, so
they fall through to generic HTTP fetch rather than failing inside the fast path. HF has no
analogous URL shape. Gatedness, privacy, and existence are all **response** properties, learned
from the API call, never inferable from the URL string. Classifying
`huggingface.co/<org>/<model>/blob/<rev>/<file>` is pure string parsing, so it satisfies
`detection.py`'s dependency-free contract and should live there with the other predicates.

**Three further implementation rules, all load-bearing:**
- **`_hf_request` inherits the fast-path `guarded_fetch` bypass.** `github.py#_github_request`
  builds a bare `httpx.AsyncClient(timeout=30.0)`, so mirroring it skips the Content-Length gate,
  the streaming size cap, and the 60 s wall-clock deadline. That is the documented, accepted state
  for every fast path (see the *Outbound fetch hardening* entry in `.claude/TECH_DEBT.md`); add
  `huggingface.py` to that entry's module list rather than treating it as a new finding.
- **Test key presence, never truthiness, when reading the `config` expand.** HF projects
  `config` through a field whitelist, so a *present but emptied* dict is a real and distinct
  signal. §14.1 states the rule and the two antipatterns that destroy it. The rule applies one
  level up as well: `config` **itself** projects to `{}` on GGUF-only repos
  (`unsloth/DeepSeek-V3.1-GGUF`), and `baseModels` is `null` rather than absent or `[]` when a
  repo declares no lineage.
- **Several expands are shaped differently than their names suggest.** `childrenModelCount` is a
  **dict**, not an integer (§11), and `baseModels` is a single object carrying one `relation` plus
  a `models` list, not a list of related models. Read §13's shape catalogue before typing an
  accessor.

## 3. URL taxonomy → `HFUrlMatch.kind`

| URL shape | kind | notes |
|---|---|---|
| `/<org>/<model>` | `model` | flagship; the beta lives here |
| `/<org>/<model>/tree/<rev>[/path]` | `tree` | directory listing |
| `/<org>/<model>/blob/<rev>/<file>` | `file` | "view" URL |
| `/<org>/<model>/resolve/<rev>/<file>` | `file` | raw/CDN URL — same handler |
| `/<org>/<model>/commit/<sha>` | `commit` | revision pin |
| `/datasets/<org>/<name>[/…]` | `dataset` | out of scope for v1 — deferred, see §11 |
| `/spaces/<org>/<name>[/…]` | `space` | out of scope for v1 — deferred, see §11 |
| `/<org>` (no repo) | `org` | list org's models |

Disambiguation: `datasets/` and `spaces/` prefixes select repo type; a bare `/<org>` with no
second path segment is an org/user. `<rev>` defaults to `main`; a 40-hex segment is an immutable
commit (cacheable forever — see §10).

## 4. Actions (mirror GitHub's `action`/`query` surface)

- **`model`** (≈ `repo`): model card + the full metadata beta. *The centerpiece.*
- **`file`**: fetch a repo file with **content-type-aware** handling (§6).
- **`tree`**: directory listing with per-file size + LFS sha.
- **`search`**: `GET /api/models?search=&author=&filter=&sort=` — surface candidate repos.
- **`org`**: list an org/user's models.

URL auto-detection in `query` (as `github()` does for issue/PR): a pasted `huggingface.co/…`
URL routes to the right kind without the caller picking an action.

## 5. The beta — `kind=model` frontmatter envelope

**One primary call** frontloads almost everything (all fields below confirmed live against
`/api/models/<repo>?expand=…`):

```
GET /api/models/<repo>?blobs=true
    &expand=safetensors&expand=config&expand=gated&expand=private&expand=disabled
    &expand=cardData&expand=baseModels&expand=childrenModelCount&expand=siblings
    &expand=tags&expand=library_name&expand=gguf&expand=sha&expand=lastModified
```

**One round-trip, ~55 ms** (measured, §13). `blobs=true` coexists with `expand` and upgrades
`siblings` to carry `size` **and** `lfs.sha256`. Returns (verified):
`safetensors:{parameters:{<dtype>:N,…}, total:N}`, `config:{architectures, model_type,
quantization_config, …}`, `gated`/`private`/`disabled`, `cardData` (incl. `base_model`),
`baseModels` (structured lineage + `relation`), `childrenModelCount`, `siblings` (+size+sha),
`tags`, `library_name`, `gguf` (full GGUF metadata when applicable), `sha`, `lastModified`.

This single response is also the sole input to every **Tier 0 quant-quality detector** (§14.1) —
those detections cost nothing beyond this call.

### Example envelope (real data, `mlx-community/DeepSeek-V4-Flash-8bit`)

```
---
source: https://huggingface.co/mlx-community/DeepSeek-V4-Flash-8bit
api: HuggingFace
repo: mlx-community/DeepSeek-V4-Flash-8bit
model_type: deepseek_v4
params: 284B                       # safetensors.total, humanized
weights: sharded — 33 shards, model.safetensors.index.json present
size: 144.44 GiB                   # canonical checkpoint set only, NOT Σ all .safetensors (§14.1)
quant: affine 8-bit                # from config.quantization_config
dtype_fingerprint: U32 36.4B + U8 8.66B + BF16 0.27B    # quant tell without a download
library: mlx
gated: false                       # tri-state: false | auto | manual (NOT a bool — §5 catalogue)
base_model: null                   # not in cardData — check card body / see_also
hint: full config + weight_map via the file action (config.json, model.safetensors.index.json); a .safetensors header can be range-read for per-tensor dtype/shape without pulling the shard
see_also: sibling quants of this family via search action → author:mlx-community "DeepSeek-V4-Flash"
shelf: 1 tracked — use ResearchShelf to review
---
┌─ untrusted content
│  # <model card markdown, fenced> …
```

### Field catalogue

The `file` action's `config.json` block is catalogued separately in §6a and §6b, since it derives
everything from the config bytes rather than the API.

**Factual (frontmatter-safe):** `source`, `api`, `repo`, `model_type`, `architecture`,
`params` (humanized `safetensors.total`), `dtype_fingerprint` (the by-dtype map — a compact
quant signature), `size` (bytes of the **canonical checkpoint set**, GiB — never Σ all
`.safetensors`; see §14.1 precondition 2), `weights` (single vs sharded + index
presence), `quant` (method/bits/mode/group_size summarized from `quantization_config`),
`library` (mlx / transformers / gguf, from tags/files), `gated`/`private`/`disabled`,
`base_model` (validated repo-path), `revision`, `downloads`, `lastModified`.

**`gated` is a tri-state string, not a bool.** Live values are `false`, `"auto"`, and
`"manual"` (verified: `meta-llama/Llama-3.1-8B-Instruct` and `google/gemma-3-27b-it` both report
`"manual"`). Render the enum rather than coercing to a bool, because the distinction is the
actionable part: `auto` is instant click-through, `manual` is a human approval queue measured in
hours to days. `if gated:` happens to work on both strings; `gated is True` silently never fires.

**Steering (`hint`/`see_also`/`warning`, appended):**
- Pointer to `file` action for `config.json` / `*.index.json` (pre-answers the next fetch).
- **Range-read recipe** for `.safetensors` headers (dtype/shape without the shard).
- **Sibling-quant discovery** — for a quant-tagged repo, a `see_also` naming the search that
  surfaces the family (the single most repeated move in this project's quant work). Gated
  behind a quant heuristic to bound latency (see §7).
- **Lineage → native-format bridge:** if `base_model` is set, a `see_also` to fetch it and a
  reminder that FP4/FP8/INT4-native bases change the quant calculus (ties into the `quant-eval`
  skill's grid-family rules).

## 6. `kind=file` — content-type-aware handling

The interception's biggest wins are here.

| File | Behavior |
|---|---|
| `config.json` | parse; frontload `model_type`, `architectures`, `checkpoint_format` (§6a), the core dimensions, attention shape, MoE counts, vision/audio presence, and a `quantization_config` summary; body = pretty JSON (fenced). |
| `*.safetensors.index.json` | frontload shard count, Σ size, `metadata.total_size`, key-count; `hint`: keys map tensors→shards; headers range-readable. Body = weight_map (fenced, possibly sliced — it's large). **More than one `*.index.json` in `siblings` means the repo ships multiple checkpoint sets** (§14.1 precondition 2); name which one this is. Do not confuse `model_index.json` (a diffusers *pipeline* manifest) with `model.safetensors.index.json` — the names near-collide and mean different things. |
| `tokenizer_config.json`, `generation_config.json`, `*.json` | parse + fenced JSON. |
| `README.md` | markdown (fenced); frontload `base_model`, `tags`, `gated` from the API. |
| **`*.safetensors` (weights)** | **never download.** Frontload LFS `sha256` + size + a `warning` that this is a multi-GB shard, plus the exact `Range: bytes=0-<n>` header-read recipe (8-byte length prefix → JSON header → dtype/shape). |
| `*.gguf` | frontload GGUF quant type parsed from filename (e.g. `Q4_K_M`) + size + sha; no download. |
| generic | raw content with a size guard (as `_action_file` does). |

### 6a. `checkpoint_format` — naming the format from config.json alone

The `file` action reads `/{repo}/raw/{rev}/{path}` from the site, never the API, so it has no
`library_name` and no `tags`. Everything it reports is derived from the config bytes. That is
fine for most formats and awkward for exactly one.

**Every quantizer in the transformers ecosystem self-identifies.** A 483-config survey across 68
tag buckets found 24 distinct `quantization_config.quant_method` values (`awq`, `gptq`,
`bitsandbytes`, `compressed-tensors`, `hqq`, `torchao`, `aqlm`, `quanto`, `exl2`, `exl3`, `vptq`,
`spqr`, `eetq`, `bitnet`, `auto-round`, `quark`, `modelopt`, `smooth_quant`, `mxfp4`, `fp8`,
`w8a8_fp8`, `w4afp8`, `nvfp4_aqlm_hybrid`, `inkling_nvfp4_aqlm_hybrid`). TensorRT-LLM
self-identifies too, in `quantization.quant_algo`. **mlx-lm declares neither**, so MLX is the one
format that has to be recognised by shape: mlx-lm writes its block twice, as `quantization` for
MLX's own loader and as `quantization_config` for HF-ecosystem compatibility.

The ladder, strongest declaration first:

| # | Test | Result |
|---|---|---|
| 1 | `quantization_config.quant_method` | that value, verbatim |
| 2 | `"quant_algo" in quantization` | `TensorRT-LLM (<algo>)` |
| 3 | `quantization == quantization_config`, both non-empty | `MLX (<mode>)` |
| 4 | no quant block of any kind, plus `dtype` / `torch_dtype` | `<DTYPE> (unquantized)` |
| 5 | otherwise | emit nothing |

**Rule 3 is content equality, and that is the entire safety margin.** TensorRT-LLM checkpoints
carry a top-level `quantization` dict and *no* `quantization_config`, so a rule testing mere
presence would label every one of them MLX. `rungalileo/mistral-7b-instruct-v0.3-trtllm-ckpt-bf16`
is the repo that fails only that gate; `glux-cz/Qwen3-8B-NVFP4-Blackwell` and
`Shoolife/Qwen2.5-1.5B-Instruct-TensorRT-LLM-Checkpoint-FP16` share its shape. Re-run through the
implemented ladder against 471 live configs: 37 MLX labels, **zero** false positives, and all three
TRT-LLM repos named rather than merely dodged.

**Rule 2 matches the key, not the value.** An unquantized TRT-LLM checkpoint writes the same block
with `quant_algo: null`. Matching the value would let it fall past rule 2, which leaves the trap
closed by luck (rule 3's mirror) rather than by construction.

Two silences are deliberate:

- **Pre-mirror MLX.** 2024-vintage mlx-lm wrote `quantization` alone, in the same shape TRT-LLM
  uses (`mlx-community/phi-2-4bit`, `Llama-2-7b-chat-4-bit`, `zephyr-7b-beta-4bit`, and 9 others in
  the sample, all `{bits, group_size}`). They go unnamed rather than risk the reverse error. The
  discriminator exists if this ever matters (TRT-LLM always carries `quant_algo`, old MLX never
  does), but it trades content equality for a keyset argument.
- **Rule 4 requires the *absence* of a quant block.** On a quantized repo `dtype` is the compute
  dtype, not the storage width, so reporting it would describe the checkpoint wrongly.

Empty blocks do not attribute: `{} == {}` is true and says nothing about who wrote it. This is a
different question from §14.1b's presence test, which asks whether a loader can construct
quantized layers and stays presence-only.

### 6b. Non-quant models get the same treatment

`checkpoint_format` covers 93% of sampled configs (439 of 471), and 69 of those labels are
`(unquantized)` on repos that previously carried no format line at all. The rest of the block is
shaped by two measurements:

- **Dimensions nest.** 61 of 99 sampled popular and recent releases carry a `text_config`, and for
  60 of those the nested block holds strictly more core dimensions than the top level. Reading only
  the top level emitted *zero* dimension fields for 54 of 99, the whole Qwen3.5 / Qwen3.6 / Gemma 4
  / Qwen3-VL line among them. `dimensions_from` names the block when it is not the top level, so a
  nested read is never mistaken for a flat one. A wrapper also carries a `vision_config` with its
  own `hidden_size`, and only `text_config` is ever descended into.
- **Expert counts have three spellings.** `num_experts` (Mixtral-era), `n_routed_experts`
  (DeepSeek, GLM, MiMo), `num_local_experts` (gpt-oss). Reading the first alone missed 7 of 15
  sampled MoE configs, each of which then reported an active-expert count with no total beside it.

Also frontloaded, each because it changes a decision the caller would otherwise make blind:
`kv_heads` with the GQA ratio (KV-cache sizing), `rope_scaling` with `factor` and the
pre-extension window (an extended and a native context report the same `max_position_embeddings`),
`sliding_window` and `attention_pattern` for hybrid stacks, and `modalities`.

`kv_lora_rank` suppresses the MHA/GQA/MQA label in favour of `MLA`: latent attention sets
`num_key_value_heads` equal to `num_attention_heads` (`deepseek-ai/DeepSeek-R1`: 128 and 128), so
the ratio reads as MHA on the model with the *smallest* KV cache in the sample.

## 7. Dead-end predictions (highest-value betas)

Each item below is a wasted tool call this design *pre-empts*, drawn from real dead-ends hit
during this project:

1. **Gated repo → 401.** This splits into two cases with different achievable outputs. Do not
   write the handler as though it were one.

   **1a. Gated but visible** (the common case: `meta-llama/*`, `google/gemma-*`). The metadata
   call succeeds and `gated` returns `"manual"` or `"auto"`, so the full envelope plus a warning
   is achievable exactly as designed:
   ```
   ---
   repo: meta-llama/Llama-3.1-8B-Instruct
   gated: manual
   warning: gated (manual approval) — /resolve/ and file reads 401 without granted access; request at https://huggingface.co/meta-llama/Llama-3.1-8B-Instruct or set HF_TOKEN
   ---
   ```

   **1b. Gated and invisible, private, or nonexistent.** The metadata call itself returns
   `HTTP 401 {"error":"Invalid username or password."}` with **no body**, so there is no `gated`
   field to read and no envelope to build. A **nonexistent repo returns the byte-identical 401**
   (verified: `XiaomiMiMo/MiMo-V2.5-Flash`, `-LM`, `-Omni`, and a fabricated repo name all
   produce the same response; the HTML page 401s too). Unauthenticated, these three states are
   **not distinguishable**. Per the frontmatter standard this is an error string carrying no
   frontmatter, and it must be honestly ambiguous rather than guessing:
   ```
   Error: HTTP 401 from the Hub for 'XiaomiMiMo/MiMo-V2.5-Flash'. The repo is gated,
   private, or does not exist — the Hub returns an identical 401 for all three when
   unauthenticated. Set HF_TOKEN to disambiguate.
   ```
2. **Multi-GB shard download.** `file` on `*.safetensors` returns the header-range recipe + sha,
   never the payload. *(This project range-read headers to read dtypes/shapes without pulling
   1 GiB tensors.)*
3. **`base_model` missing from `cardData`.** Fall back to parsing the card body's `base_model:`
   line and surface it; don't leave the agent to re-fetch the README.
4. **Sharded vs single.** Presence of `index.json` tells the agent whether to expect a
   `weight_map` — pre-declared in `weights:`.
5. **Model-type / library mismatch.** From `tags`/files, declare `library` (mlx / gguf /
   transformers) so the agent knows the loader family before trying one. The stronger signal
   ("this `model_type` has no loader module") is **not** available to Parkour; see detector #8 in
   §14.1 for what to emit instead and why.
6. **Revision drift.** Surface `lastModified` + resolved default revision; echo a pinned commit
   when the query carries one (immutable → safe to trust/cache).

## 8. HF API endpoint map

| Purpose | Endpoint |
|---|---|
| Flagship model metadata | `GET /api/models/<repo>?expand=safetensors,gated,config,cardData,siblings,tags,downloads,lastModified` |
| LFS shas + sizes | `GET /api/models/<repo>?blobs=true` (`siblings[].lfs.sha256`) |
| File tree | `GET /api/models/<repo>/tree/<rev>?recursive=1` |
| Branches / tags | `GET /api/models/<repo>/refs` |
| Search | `GET /api/models?search=&author=&filter=&sort=&limit=` |
| File content | `GET /<repo>/resolve/<rev>/<file>` (+ `Range` for safetensors headers) |
| LFS pointer / small raw | `GET /<repo>/raw/<rev>/<file>` |
| Auth | `Authorization: Bearer <HF_TOKEN>` — optional; unlocks gated/private + higher limits |

**Resolved by probing (§13):** `expand` is **repeatable** (`&expand=a&expand=b`), coexists with
`blobs=true`, and an invalid token returns a `400` whose error body enumerates the full valid
set (a useful self-documenting endpoint). **`securityStatus` is not a valid token** — the
scan-status beta idea is dropped. The authoritative token list is in §13.

**Rate-limit headers (resolved — the answer is not GitHub's shape).** HF sends no `X-RateLimit-*`.
It sends RFC 9651 structured fields:

```
ratelimit: "api";r=495;t=140
ratelimit-policy: "fixed window";"api";q=500;w=300
```

500 requests per 300 s fixed window, per named bucket (`"api"`). `_hf_rate_limit_warning()` needs
a small structured-field parser reading params that trail a quoted bucket name, not `int(header)`.

**A 401 on the metadata call is ambiguous** between gated-invisible, private, and nonexistent
(§7 dead-end 1b). Do not synthesize a `gated: true` envelope from a 401.

**Still to verify at implementation time:** the `datasets-server` surface if datasets are
promoted (§13).

## 9. Fetch interception (`_hf_fast_path`)

Mirror the arXiv/wiki fast paths in `fetch_direct.py`: a `huggingface.co` URL asked of the
generic web-fetch tool routes to the HF handler instead of scraping the JS SPA:
- model page → the model beta (not HTML).
- `resolve`/`blob` of a JSON config/index → parsed + frontloaded (§6).
- `resolve` of `*.safetensors`/`*.gguf` → header/metadata recipe, never a full download.
Falls through to the generic pipeline on any failure (the `_arxiv_fast_path` "must not swallow
silently" convention).

## 10. Caching

Model metadata is cacheable in the existing 2Q `_PageCache`. Key on `(repo, revision)`.
Commit-pinned revisions (40-hex) are **immutable** — cache indefinitely; `main`/branch refs get
the normal TTL. A cached model-metadata entry also serves the follow-up `file`/`tree` betas
without re-hitting the API.

## 11. Decisions — resolved by probing

Recorded here so implementers don't re-litigate them; evidence in §13/§14.

| Decision | Resolution |
|---|---|
| Datasets / Spaces in v1 | **Models only.** Datasets = earmarked fast-follow (needs the separate `datasets-server` API — schema/features/splits/first-rows; a genuinely different beta). Spaces = not a resource; but `expand=spaces` on a *model* is a free demo cross-ref. |
| Sibling-quant discovery cost | **Gate it, but read the gate correctly — see the `childrenModelCount` note below this table.** The naive form of this gate never fires on the repos it was designed for. Default to emitting the search string as a zero-cost `see_also`; execute the +53 ms search only when the count is both present and non-zero. |
| GGUF depth | **`expand=gguf` (free) + filename parsing.** No ranged header read needed. |
| `dtype_fingerprint` presentation | **Raw by-dtype counts** beside the `quantization_config` summary. Don't over-interpret across formats — but *do* derive the mechanical signals in §14.1 (E8M0 presence, bpw). The by-dtype counts are unconditionally safe to print; `bpw` is not, and is only meaningful once §14.1a's four preconditions pass. |
| Security-status surfacing | **Dropped** — `securityStatus` is not a valid `expand` token. |
| Proactive header reads | **Never.** ~500 ms (10× an API call); emit the range-read recipe instead (§14.3). |
| Token convention | Mirror the GitHub tool's precedent exactly: `common.py#load_credential("HF_TOKEN", HF_CONFIG_PATH)` with `HF_CONFIG_PATH = ~/.config/parkour/hf_token`, cached per session like `github.py#_get_github_token`. Keep the path a module-level constant (it is the test seam `load_credential` documents). Optional — it unlocks gated/private repos and higher rate limits. |

**⚠ `childrenModelCount` is a dict, and it counts the wrong repo.** Two independent traps:

1. **Shape.** The live value is `{"adapter": 0, "merge": 0, "quantized": 0, "finetune": 0}`, not an
   integer. `count > 0` raises `TypeError`; `if count:` is unconditionally true, because a non-empty
   dict is always truthy. Read `count.get("quantized", 0)` — and note this is the same
   presence-vs-truthiness discipline as §14.1b, arriving through a different door.
2. **Direction.** Every quant repo probed reports **all zeros**, because a quant *is* the child.
   The counts that matter live on the **base**: `deepseek-ai/DeepSeek-V4-Flash` reports
   `quantized: 112`, `XiaomiMiMo/MiMo-V2.5` reports `29`. So a gate reading the *current* repo's
   count never fires for the sibling-quant discovery case it exists to serve.

Read it as a **base-model** signal, not a self signal: when the repo has children (it is a base),
`quantized > 0` justifies the search. When the repo is itself a quant (`baseModels.relation ==
"quantized"`), its own count is uninformative and the family is better reached by an author- and
name-scoped `see_also` string, which costs nothing. Do not spend a call to fetch the base purely to
read its child count; Tier 1 already fetches the base for better reasons (§14.2), so let the count
ride along when Tier 1 is on and stay a `see_also` when it is off.

## 12. Why this is a strong Parkour fit (summary)

- **API-first:** replaces SPA scraping with the Hub REST API (principle #1/#2).
- **Interception:** `huggingface.co` URLs transparently upgrade (principle #2).
- **Beta with teeth:** one call pre-answers the config/params/size/gated/lineage questions and
  *predicts the dead ends* (gated 401, GB shard pulls) that this very project kept hitting
  (principle #3). The model-repo "what next" graph is regular enough that the beta hit-rate
  should be high.
- **Judgement, not just retrieval:** §14 shows the envelope can also tell the driver whether a
  quant is *trustworthy* — effective bits-per-weight, whether a native FP grid survived, whether
  the config is too under-specified to load — from arithmetic on data the flagship call already
  returned. That is the beta concept at its strongest: not "here is the page", but "here is what
  you were about to spend five calls figuring out, and here is the trap in it."

*(Measured latencies, the authoritative `expand` list, and the detector design follow in
§13–§14.)*

---

## 13. Empirical findings (probed live — resolves §11)

**Latency (median of 3–5, warm):**

| Call | Latency | Role |
|---|---|---|
| Flagship `?blobs=true&expand=…` (1 round-trip) | ~55 ms | the entire default beta |
| Sibling-family search `/api/models?search=&author=` | ~53 ms | conditional 2nd call |
| Full `config.json` resolve | ~85 ms | on-demand (`file` action) |
| safetensors header range-read (2 MB) | **~500 ms** | on-demand ONLY (CDN redirect + TTFB) |

**Design consequences:**
- **The default model beta is ONE call (~55 ms).** `blobs=true` coexists with `expand`
  (the earlier combined-call error was the invalid `securityStatus` token, not a conflict);
  with `blobs=true`, siblings carry `size` **and** `lfs.sha256`. So one request returns
  params + dtype fingerprint, quant summary, gated/private, `base_model`, per-file sizes +
  shas, library, GGUF metadata, commit sha. No second call for defaults.
- **Header reads are ~500 ms** — 10× an API call. Never proactive; emit the range-read recipe
  + LFS sha and let the agent pull a header only when it needs per-tensor dtype/shape.
- Use the `config` **expand** (free, in the flagship call) for the quant *summary*; fetch full
  `config.json` (`file` action, +85 ms) only when the per-module quant map is needed.

**Authoritative valid `expand` tokens** — 32, quoted from the API's own 400 body: `author,
baseModels, cardData, config, createdAt, disabled, downloads, downloadsAllTime, evalResults,
gated, inference, inferenceProviderMapping, lastModified, library_name, likes, mask_token,
model-index, pipeline_tag, private, safetensors, sha, siblings, spaces, tags, transformersInfo,
trendingScore, widgetData, gguf, resourceGroup, xetEnabled, childrenModelCount, usedStorage`.
**`securityStatus` is NOT valid** → drop the scan-status beta. (Re-read the 400 body at
implementation time rather than trusting this list; it is a live enumeration and will drift.)

**Newly useful, free-in-the-flagship expands** (shapes quoted verbatim from live responses —
several are not what the field name suggests):
- **`baseModels`** — structured lineage (prefer over scraping `cardData.base_model`);
  `base_model` confirmed present in `cardData` for repos that declare it. **Shape:** a single
  object, `{"relation": "quantized", "models": [{"_id": "…", "id": "XiaomiMiMo/MiMo-V2.5"}]}` —
  one `relation` for the whole record plus a `models` **list**, not a list of per-model relations.
  The base id is `baseModels["models"][0]["id"]`. **`null`** (not `[]`, not absent) when the repo
  declares no lineage, so detector #10 needs a `None` check.
- **`childrenModelCount`** — derived-model counts, **a dict keyed by relation**
  (`{"adapter": …, "merge": …, "quantized": …, "finetune": …}`), not an integer, and it counts
  *this* repo's children rather than its siblings. See the ⚠ note under §11's table before wiring
  any gate to it.
- **`gguf`** — full GGUF metadata (total params, arch, context, imatrix) for GGUF repos →
  GGUF beta needs no header read; filename parsing covers per-file quant type.
- **`spaces`** — Spaces that demo this model → free `see_also` cross-ref.
- **`evalResults`** — model-index benchmark scores → optional "how good is it" beta.

**§11 resolutions:**
- Sibling discovery → default to the zero-cost `see_also` search string; execute the live search
  only when a *base-model* `childrenModelCount["quantized"]` is non-zero and already in hand
  (§11's ⚠ note explains why the current repo's own count is the wrong number to read).
- GGUF → `expand=gguf` + filename parsing; no ranged read.
- dtype fingerprint → keep raw by-dtype counts beside the `quantization_config` summary; don't
  over-interpret across formats.
- Security status → not exposed via expand; dropped.

**Datasets / Spaces (V1 = models only):**
- **Datasets** = clean fast-follow (schema / features / splits / sample rows via the separate
  `datasets-server` API: `/api/datasets/<repo>/parquet`, `/first-rows`). Real LLM value for
  data/eval code; different beta surface.
- **Spaces** = not a resource in V1 (app source is a file read; runtime/hardware is niche). But
  `expand=spaces` on a *model* is a free demo cross-ref worth surfacing.

---

## 14. Bad-quant detection in frontmatter (validated against known-good/known-bad repos)

Most red flags from the `quant-eval` skill are computable from the **flagship response the tool
already fetches** — zero marginal latency. The expensive ones become opt-in.

### 14.1 Tier 0 — FREE (no extra calls; derived from the flagship JSON)

**Formulas**
- `bytes = Σ (canonical checkpoint set).size` (from `blobs=true`; **not** Σ all `.safetensors` —
  see precondition 2)
- `elems = Σ safetensors.parameters[dtype]` (stored array elements)
- **`bpw = bytes*8 / safetensors.total`** — effective bits per logical weight
- **`fp_grid = (U8+F8_E8M0) / (U8+F8_E8M0 + BF16+F16)`** — share of scale metadata that is E8M0
  (FP-microscaling). Both buckets, not just `U8`: MLX packs E8M0 scales into a bare `U8` array
  while a natively microscaled release reports a real `F8_E8M0` dtype, and counting only `U8`
  scores `deepseek-ai/DeepSeek-V4-Flash` at 0.0 and calls a preserved FP grid "all-affine".
  **Undefined, not 0.0, when the scales are not in the histogram.** The Hub does not guarantee
  it reports them: `deepseek-ai/DeepSeek-V4-Flash-0731` publishes *no* `F8_E8M0` bucket, yet a
  range-read of any middle shard finds 776 E8M0 tensors in it (`layers.0.attn.wkv.scale` and
  siblings) — the scales are in the file, the aggregate omits them, and the preview release of
  the same model in the same format reports them normally. Gate on `scale_like ≥ payload /
  128`: a scale array holds one entry per group, so it cannot fall under payload over the
  coarsest group size anyone uses. This is arithmetic rather than a name match because no
  naming fix reaches it; the data is absent from the response, not mislabelled in it.
- **`packing = (F8_E8M0 × 32) / (I8+U8)`** — logical weights per stored payload byte, the
  independent denominator check of §14.1a precondition 1. Unavailable (not zero) when scales
  share a bucket with the payload.

### 14.1a ⚠ Four mandatory preconditions on `bpw`

`bpw` is a ratio between two independently unreliable numbers. **Every one of these gates is
mandatory, and each one has a verified repo that fails only that gate.** Emitting `bpw` without
all four produces a false accusation of "bloat" against an honest release, on repos as prominent
as `openai/gpt-oss-120b` and `mistralai/Mistral-Small-3.2-24B-Instruct-2506`.

**1. Denominator validity — `safetensors.total` is sometimes storage elements, not logical
weights.** HF's parser *sometimes* unpacks sub-byte weights and sometimes does not:

| repo | Σ dtypes | `total` | ratio | unpacked? | bpw |
|---|---|---|---|---|---|
| `mlx-community/DeepSeek-V4-Flash-8bit` | 45.4B | 284.3B | 6.26 | yes | **4.36** ✔ |
| `inferencerlabs/MiMo-V2.5-LM-MLX-Q9` | 96.5B | 308.8B | 3.20 | yes | **9.00** ✔ |
| `dawncr0w/MiMo-V2.5-oQ4-MLX` | 50.1B | **50.1B** | **1.0000** | **NO** | 28.76 ✘ |
| `deepseek-ai/DeepSeek-V4-Flash` | 158.1B | **158.1B** | **1.0000** | **NO** | 8.08 ✘ |

**Guard:** if `U32 present AND total == Σ dtypes` then `total` is un-unpacked, so **suppress
`bpw`** or substitute the base model's `total` (Tier 1: MiMo's real base is 310.8B → oQ4 bpw =
**4.63**, correct). Emitting 28.76 bpw as "bloat" on a legitimate oQ4 is a false accusation.

Two hardenings on that test. Use **exact** equality, not a tolerance band: un-unpacked repos land
at ratio exactly 1.0000, and the minimum possible packing ratio is 2:1, so the margin is enormous
and a tolerance band only invites a tuning bug. Optionally also require U32 to be the *dominant*
bucket — it is 79–80% of Σ dtypes in all three rows above — which removes any repo carrying U32
incidentally rather than as packed weights.

Why keying on `U32` is correct rather than merely convenient: `U32` is a pure container dtype,
never a logical weight dtype, so its presence is positive evidence of packing. Contrast
`openai/gpt-oss-120b`, which is mxfp4 and also reports `total == Σ dtypes` — but HF unpacked *into*
its `U8` bucket, so Σ is already the logical count and the equality is legitimate there. Ratio 1.0
alone is therefore **not** the signal; ratio 1.0 *with U32* is.

*(The `28.76` above is Σ-all-safetensors, the pre-guard-2 figure. After precondition 2 partitions
out `audio_tokenizer/`, oQ4's canonical number is `28.66`. Both are suppressed; the table quotes
the raw arithmetic to show what an unguarded implementation would print.)*

**Residual gap, now closed — sub-byte packing in a container that is not `U32`.** The `U32` key
is sound but not exhaustive: `gpt-oss-120b` proves HF also packs into `U8`, and there it happened
to unpack. The first close for this was a cross-check on the declared width (`bits` **survives the
`config` whitelist**, §14.1b, so `bpw > ~2 × bits` means the denominator is self-evidently wrong).
That was recorded here as a *theoretical* hole because no real repo had exercised it. One has now.

`deepseek-ai/DeepSeek-V4-Flash` defeats both tests at once. It packs FP4 two-per-`I8` with the
scales in a distinct `F8_E8M0` bucket, so there is no `U32` anywhere; it lands at ratio exactly
1.0000; and it declares `quant_method: fp8` with **no `bits`**, so the declared-width cross-check
has no width to test against and degrades silently, exactly as designed. Unguarded it reports
**8.08 bpw against a true ~4.39**: a native-FP4 vendor release rendered as an 8-bit upload. That
is worse than a wrong number on one repo, because the base is the denominator of every requant
verdict drawn from it. Read straight, "base 8.08 bpw" beside "quant 4.36 bpw" says the quant threw
away half the precision, when the real comparison is ~4.39 against 4.36 with the expert grid
bit-preserved.

**Guard:** measure the packing factor from the scale array, which counts logical weights
independently of whichever bucket the payload landed in. OCP microscaling writes one E8M0 scale
per 32 weights, so `(F8_E8M0 × 32) / (I8 + U8)` is weights-per-stored-byte directly: **2.0001**
here, and 1.0 for a byte-per-weight format such as mxfp8. Suppress at ≥ 1.5, the midpoint of a gap
nothing occupies. Zero extra calls, and it needs no declared width.

**The abstention is load-bearing in both directions.** The measurement returns nothing when the
Hub folds scales into the payload bucket (`gpt-oss-120b` reports one undifferentiated `U8`), and
that silence is what lets correct numbers through. `deepseek-ai/DeepSeek-V4-Flash-0731` is the same
vendor and the same format, but there HF *did* unpack the FP4 into logical counts and dropped the
E8M0 scales from the histogram entirely; its `total` is a true weight count and its **4.39 bpw is
correct**. One family, two repos, opposite conventions, no signal on the repo itself announcing
which. Resolve the basis per repo; never assume a vendor is internally consistent.

**2. Numerator validity — `Σ siblings[*.safetensors].size` over-counts repos shipping more than
one checkpoint set.** This is the same severity as the U32 guard and fires on repos with no U32 at
all, so guard 1 never sees it:

```
openai/gpt-oss-120b                    Σ all  121.54 GiB → bpw  8.67 ✘   (trips detector #2)
                                   canonical   60.77 GiB → bpw  4.34 ✔
mistralai/Mistral-Small-3.2-24B-…      Σ all   89.45 GiB → bpw 32.00 ✘
                                   canonical   44.72 GiB → bpw 16.00 ✔
```

Two distinct duplicate classes, and **neither is byte-identical, so sha-based dedup does not
work.** `gpt-oss-120b` carries `original/` holding the same weights **re-sharded** (7 files vs 15,
all 22 `lfs.sha256` values distinct); deduping siblings by sha collapses nothing and leaves
121.54 GiB unchanged. `mistralai/*` carries `consolidated.safetensors` **at top level** beside the
sharded set, so "top-level only" is not a fix either.

**Guard:** partition `.safetensors` siblings into candidate checkpoint sets keyed on
`(directory, group)`, where `group` is the `-of-N` shard group when the filename carries one and
the **single literal bucket `"singles"`** otherwise. Then pick the canonical set — prefer
top-level over a subdirectory, and prefer a *sharded* set over a bucket of singles:

| repo | sets found | picked | result |
|---|---|---|---|
| `openai/gpt-oss-120b` | `('', of-14)` 15f · `('original', of-7)` 7f | top-level | **60.77 GiB** ✔ |
| `mistralai/Mistral-Small-3.2-24B` | `('', singles)` = consolidated · `('', of-10)` 10f | sharded | **44.72 GiB** ✔ |

Zero extra calls.

**The `"singles"` bucket is not a stylistic choice — keying singles per filename breaks a real
repo.** `XiaomiMiMo/MiMo-V2.5` ships 18 top-level `.safetensors` named
`model_pp0_ep4_shard0.safetensors`, `model_mtp.safetensors`, and friends: **no `-of-N` pattern
anywhere**. Keyed per filename, every file becomes its own checkpoint set, the ranking picks the
smallest plausible one (`model_mtp.safetensors`, 1.11 GiB), and **`bpw` reports 0.03 against a true
8.13**. A top-level `model.safetensors.index.json` *is* present, so the pipeline gate below does
**not** rescue it. This is not a corner case: MiMo-V2.5 is the base that both MiMo quants name in
`baseModels`, so §14.5's auto-enable heuristic fetches it, and §14.6's worked example would report
its own base at 0.03 bpw. Verified A/B over the sample:

| repo | expected | per-filename keys | `"singles"` bucket |
|---|---|---|---|
| `openai/gpt-oss-120b` | 4.34 | 4.34 ✔ | 4.34 ✔ |
| `mistralai/Mistral-Small-3.2-24B` | 16.00 | 16.00 ✔ | 16.00 ✔ |
| **`XiaomiMiMo/MiMo-V2.5`** | **8.13** | **0.03 ✘** | **8.11 ✔** |
| `inferencerlabs/MiMo-V2.5-LM-MLX-Q9` | 9.00 | 9.00 ✔ | 9.00 ✔ |
| `mlx-community/DeepSeek-V4-Flash-8bit` | 4.36 | 4.36 ✔ | 4.36 ✔ |
| `deepseek-ai/DeepSeek-V4-Flash` | 8.08 | 8.08 ✔ | 8.08 ✔ |
| `Qwen/Qwen2.5-VL-7B-Instruct` | 16.00 | 16.00 ✔ | 16.00 ✔ |

Corroborating free tells that a repo ships more than one checkpoint set: more than one
`*.index.json`, any `consolidated*`, or more than one distinct `of-N` group.

*Do not add "any `.safetensors` under a subdirectory" to that list.* A subdirectory holds a
genuine extra **component** at least as often as a duplicate: both `XiaomiMiMo/MiMo-V2.5` and
`dawncr0w/MiMo-V2.5-oQ4-MLX` carry `audio_tokenizer/model.safetensors` (0.61 GiB), which is part
of the model, not a second copy of it. Prefer-top-level drops it from canonical bytes (MiMo-V2.5
computes 8.11 against a true 8.13). Accept that: the error is sub-1%, and it under-counts bytes,
which biases `bpw` **downward** and therefore away from falsely accusing a repo of bloat. Trading
a bounded under-count for a bounded over-count is the right direction for a detector whose failure
mode is libel.

*Parsing trap:* **`gpt-oss` is 0-indexed** (`model-00000-of-00014` … 15 files for "of-14"), while
`mlx-community/deepseek-ai-DeepSeek-V4-Flash-8bit` is 1-indexed (65 files for "of-65"). So
"file count ≠ N" is **not** a reliable duplicate test. "More than one distinct `of-N` group" is.

*Also collapse precision-variant siblings*, a same-directory duplicate class distinct from
`consolidated`: strip a `.fp16` / `.fp32` / `.bf16` infix and check whether the stem collides
(`stabilityai/stable-diffusion-xl-base-1.0` carries `unet/diffusion_pytorch_model.safetensors`
9.56 GiB beside `unet/diffusion_pytorch_model.fp16.safetensors` 4.78 GiB, a clean 2:1).

*Pipeline shape gate.* For diffusers-shaped repos the partition has no principled answer, because
`safetensors.total` counts **one component** while `bytes` counts the whole pipeline, so no set
choice yields a meaningful ratio:

```
black-forest-labs/FLUX.1-dev              Σ all bpw  38.91   no top-level safetensors index
stabilityai/stable-diffusion-xl-base-1.0  Σ all bpw 109.81   fp16/fp32 pairs + LoRA + 2 all-in-ones
```

Free gate, one field already in `siblings`: a top-level `model.safetensors.index.json` means LLM
shape, proceed. `model_index.json` present with **no** top-level safetensors index means diffusers
pipeline — **suppress `bpw` entirely.** (The two filenames near-collide and mean different things.)

The pipeline gate rescues FLUX and SDXL, but it does **not** subsume the `"singles"` decision above:
MiMo-V2.5 passes the pipeline gate cleanly and still needs the bucket. Both guards are required.

**3. Is-this-actually-a-quant — required before any `bpw` *commentary*.** Computed correctly on a
stock unquantized release, `bpw` equals the storage width by definition, and detector #2's
threshold then fires on every BF16 model on the Hub:

```
Qwen/Qwen2.5-VL-7B-Instruct                    bpw 16.00   quantization_config absent, no lineage
mistralai/Mistral-Small-3.2-24B-Instruct-2506  bpw 16.00   quantization_config absent, relation=finetune
```

Emitting *"16.0 bpw — at or above 8-bit storage; upcast bloat if the base is FP4/INT4-native"* on a
first-party BF16 release is arithmetically true and completely astonishing. Since unquantized repos
outnumber quants on the Hub, this is the highest-frequency wrong output in the design.

**Guard:** gate detector #2 on `baseModels.relation == "quantized"` **OR** a packed container dtype
(`U32` / `U8` / `I8`) **OR** quant tags. Verified `relation` values: `quantized`
(`dawncr0w/MiMo-V2.5-oQ4-MLX`, `inferencerlabs/…-Q9`, `unsloth/*-GGUF`), `finetune`
(`Mistral-Small-3.2`, correctly excluded from bloat), `None` (`Qwen2.5-VL`, `FLUX`, `SDXL`,
`deepseek-ai/DeepSeek-V4-Flash`). `relation == "quantized"` is strictly better than "lineage
present" here, because bloat is meaningless relative to yourself and a `finetune` relation is not
a quant relationship.

*The disjunction is load-bearing — do not simplify it.* Collapsing the gate to "non-empty
`quantization_config`" suppresses **exactly `inferencerlabs/MiMo-V2.5-LM-MLX-Q9`**, the motivating
case, because its projected config is `{}` and `{}` is falsy. Q9 passes on two of the three arms
(`relation` and `U32`); `Qwen2.5-VL` passes on none. That is the shape you want.

This gate governs detector **#2 only**. Detector #5 needs the key-presence test in §14.1b instead,
and coupling #5 to this gate would be a needless dependency that risks re-suppressing the very
case #5 exists to catch.

**4. Measurable weights — a repo may have nothing to divide.** `unsloth/DeepSeek-V3.1-GGUF` has
**no `safetensors` block at all**, `bytes = 0` from 236 `.gguf` files, and
`relation == "quantized"` — so precondition 3 *passes* and hands a `None` denominator and a zero
numerator straight into the division. GGUF-only repos are extremely common and will hit this
constantly. **Guard:** absent `safetensors` block or zero canonical bytes means `bpw` is undefined;
say so explicitly. Not a crash, and not a `0`.

### 14.1b Reading the `config` expand: test presence, never truthiness

`expand=config` projects `quantization_config` through a **field whitelist**. `bits`,
`quant_method`, and `modules_to_not_convert` survive; `group_size`, `mode`, and per-module keys are
dropped. Consequence: a *present but emptied* dict is a real signal, and it is **not** the same
thing as an absent key.

| repo | key present? | projected verbatim | real `config.json` |
|---|---|---|---|
| `inferencerlabs/MiMo-V2.5-LM-MLX-Q9` | **yes** | `{}` | `{"group_size": 32}` |
| `dawncr0w/MiMo-V2.5-oQ4-MLX` | yes | `{"bits": 4}` | `{group_size:64, bits:4, mode:affine, +per-module map}` |
| `openai/gpt-oss-120b` | yes | `{"quant_method":"mxfp4", "modules_to_not_convert":[…]}` | (mxfp4) |
| `Qwen/Qwen2.5-VL-7B-Instruct`, `Mistral-Small-3.2`, `Llama-3.1-8B`, `gemma-3-27b`, `FLUX.1-dev`, `Qwen3-8B` | **no** | key absent | absent |

So the two states are cleanly distinguishable **for free**, and detector #5 stays a Tier 0 verdict
with no Tier 2 escalation:

- key present + `{}` ⇒ declared but unloadable ⇒ **warn** (the gatekeeping tell)
- key absent ⇒ not quantized ⇒ **silent**

**The correct test is `'quantization_config' in config`.** Three antipatterns destroy the only
signal that separates those rows, and each must be named because each looks idiomatic:

```python
(config or {}).get("quantization_config", {})   # WRONG: absent becomes {}
if not qc: ...                                  # WRONG: {} and absent both falsy
qc = c.get("quantization_config") or c.get("quantization")   # WRONG: {} falls through to None
```

This is not a hypothetical. **The third line is real: it ran during this spec's own probing and
produced a false conclusion that HF projects Q9's config to `null`, which very nearly wrote a
spurious Tier 2 escalation into this document.** If the trap caught the spec's author mid-design,
it will catch an implementer, which is why the rule is stated rather than mentioned.

*Scope the warning to `bits` specifically.* Because `bits` **survives** the whitelist whenever it
is present, absence of `bits` in the projection reliably implies absence of `bits` on disk. But
`group_size` and `mode` are dropped, so nothing can be inferred about them for free — a repo
declaring `{mode: "affine", group_size: 64}` and no bits also projects `{}`. Phrase the warning as
**"declares a quantization block with no `bits`"**: always true, free, and still exactly the
load-blocking condition, since no loader can construct a quantized layer without a bit width. The
looser "no bits/mode" over-claims on the one field the whitelist hides.

Corollary for future detectors: `{}` is partly a *projection* artifact, not purely an uploader
one. Q9's uploader did declare `{group_size: 32}`. Never infer uploader intent from an emptied
whitelisted field.

**Detections**

| # | Signal | Source | Meaning |
|---|---|---|---|
| 1 | `bpw` vs declared `bits` | config+safetensors | large gap ⇒ **mixed-precision** (informational, not bad — see 14.4) |
| 2 | `bpw ≥ ~8.4` | derived | at/above 8-bit storage; **upcast bloat** if the base is FP4/INT4-native. **Requires all four preconditions (§14.1a); gate 3 is specifically this detector's.** |
| 3 | **`fp_grid` high (U8/E8M0 present)** | dtype fingerprint | an FP-microscaling grid is **present in this repo** (mxfp4/mxfp8 scales). **Not a preservation verdict:** whether it matches the base's grid needs the base fetched, so the comparative claim belongs to Tier 1 (§14.5) and nowhere else. Asserting "preserved" from this signal alone fired on vendor *base* repos about their own native format, and fired beside Tier 1's own "declares no base model, so there is no native format to compare against" in the same response. |
| 4 | **`fp_grid` ≈ 0 + U32 bulk** | dtype fingerprint | **all-affine**: scales are float, so *if* the base carried an FP grid it was regridded. Same hedge as #3, for the same reason. **Only when the scales were reported at all** (§14.1): a 0 read off an absent bucket is not a measurement, and on the Tier 1 side it becomes a false all-clear that green-lights the very regrid this signal exists to flag. `fp_grid` is `None` there and the audit says it cannot tell. |
| 5 | **`quantization_config` present but `{}`** (NOT absent — §14.1b) | config expand | **gatekeeping tell** — declares a quantization block with no `bits`, so stock loaders cannot construct quantized layers. Absent key means unquantized: stay silent. |
| 6 | this repo's *own* native format | dtype fingerprint | `I8`+`F8_E8M0` ⇒ FP4-packed; `F8_E4M3` ⇒ FP8-native; `U32`+`BF16` (no E8M0 bucket) ⇒ affine, since float scales are affine's signature. **`U32`+`U8` does NOT resolve to a mode:** pure mxfp4/mxfp8 and an affine/mx hybrid emit identical buckets, and an affine share small enough to hide in BF16 leaves no trace. Measured on one family, `Vontra/DeepSeek-V4-Flash-0731-MXFP4-MLX` (no affine module anywhere) scores `fp_grid` 0.93 while `mlx-community/DeepSeek-V4-Flash-8bit` (affine 8-bit backbone under mxfp4 experts) scores 0.97, so the ordering runs backwards and no threshold recovers it. Name the grid and leave `mode` to `config.json`. Guessing wrong is not cosmetic: on an FP4-native base, "affine" is the string that signals a destructive regrid. |
| 7 | `gated` / `private` / `disabled` | expand | 401 dead-end before it happens |
| 8 | remote-code / loader-resolution risk | `config.auto_map`, `transformersInfo` vs `library_name` | load dead-end. **A true "`model_type` has no loader module" check is not implementable here** — it needs mlx-lm's `MODEL_REMAPPING` or transformers' `CONFIG_MAPPING`, and Parkour has no ML dependency (`pyproject.toml` declares no mlx, transformers, safetensors, or huggingface package) nor should it acquire one for this. Emit the free signals instead: `config.auto_map` present means `trust_remote_code=True` is required (a genuine dead-end, present on both MiMo repos as `AutoConfig: configuration_mimo_v2.MiMoV2Config`), and empty `transformersInfo` while `library_name: transformers` is the coherence tell detector #9 already wants. Do not ship a static `model_type` allowlist; it goes stale immediately. |
| 9 | `library_name` vs `model_type` coherence | expands | wrong-loader-family warning |
| 10 | lineage present? | `baseModels` | enables Tier 1; absent ⇒ say so, don't silently omit |

**Validated true positives (real output from the prototype):**
- `mlx-community/deepseek-ai-DeepSeek-V4-Flash-8bit` → `bpw 8.51` + `all-affine`, vs sibling
  `mlx-community/DeepSeek-V4-Flash-8bit` at `bpw 4.36` with **identical `total` 284.3B** ⇒ same
  params, 2× bytes = the strictly-dominated upcast, caught for free. *(Both live under
  `mlx-community`; the org prefix matters, since the flattened `deepseek-ai-…` fragment is part of
  the repo name, not an org.)* The 8.51 repo survives precondition 2 on inspection — a single
  `('', of-65)` set, 65 files, 281.51 GiB, one index — so the 2× is genuine upcast and not a
  re-shard.
  **⚠ This true positive and precondition 2's false positive are the same arithmetic signature:**
  "identical `total`, 2× bytes" is exactly what a duplicate checkpoint set produces
  (`mistralai/Mistral-Small-3.2-24B` reports precisely 2× against a correct denominator). The
  partition of precondition 2 is what separates genuine upcast bloat from a re-shard, and it must
  run *before* this comparison is trusted. Do not read a 2× byte ratio as bloat on its own.
- `inferencerlabs/MiMo-V2.5-LM-MLX-Q9` → `quantization_config` **present and `{}`** (gatekeep,
  §14.1b) + `bpw 9.00`.
  The arithmetic also *explains the branding*: BF16 overhead = 19.35B/308.8B = 0.0625
  elems/param = exactly 2 BF16 arrays per 32 weights ⇒ **8-bit affine + scale AND bias at
  gs32 = 8 + 32/32 = 9.0 bpw**. "Q9" is effective-bpw branding, not a 9-bit format.
- Native bases self-describe: `deepseek-ai/DeepSeek-V4-Flash` → `I8 141.7B + F8_E8M0 8.9B +
  F8_E4M3 6.0B` (FP4 experts + FP8 backbone); `XiaomiMiMo/MiMo-V2.5` → `F8_E4M3 306.7B`
  (pure FP8). Matches omlx's `_classify_pair` classification rules exactly.

### 14.1c The generalized rule (why the preconditions exist)

Every detector in this suite reads one or more of three inputs, and **all three are independently
lossy, each in a different way**:

| input | failure mode | what it means |
|---|---|---|
| `safetensors.total` | **magnitude and scope** | may be storage elements not logical weights (precondition 1), or one component not the checkpoint (pipeline gate) |
| `siblings` | **set membership** | which files constitute *one* checkpoint (precondition 2) |
| `config` | **whitelist projection** | absence of a field is not absence on disk; present-but-emptied is its own signal (§14.1b) |

So: **gate per input a detector touches, and test presence rather than truthiness.** Absence of
evidence is never evidence of absence in any of the three, and in `config` it is specifically the
*emptied-yet-present* case that carries the signal.

That is the U32 guard's discipline generalized. The U32 guard is not a special case to remember,
it is the first instance of the rule; every new detector added to this suite needs the same
treatment for whichever inputs it reads.

### 14.2 Tier 1 — CHEAP OPT-IN (+~55 ms per call)

- **Base-model native format** (fetch base's flagship; the id is free at
  `baseModels["models"][0]["id"]` — §13 has the shape) ⇒ the
  **grid-preservation verdict** (native FP4 → affine = damage; → mxfp4 = preserved; 8-bit-native
  → anything 8-bit = benign) **and** a trustworthy param count to fix `bpw`. *Highest value per
  millisecond in the whole design.*
- **Sibling-quant family** (search by base/name) ⇒ comparative bpw table → bloat becomes
  relative and provable rather than heuristic.

### 14.3 Tier 2/3 — DEFERRED (recipe hints only, never proactive)

| Tier | Cost | Buys |
|---|---|---|
| 2 | +85 ms (`config.json`) | `mode` + `group_size` + per-module quant map — definitive per-component grid verdict (`{mode:mxfp4,gs:32}` on experts). **Needed because `expand=config` returns only `{"bits": N}`** — mode/gs are absent from the free summary. |
| 2 | +85 ms (`*.index.json`) | MTP / vision / audio tensor presence ⇒ **silent-stripping** detection; key count; shard map |
| 3 | **~500 ms** (header range-read) | per-tensor dtype/shape; definitive `.biases` presence |
| 3 | N×55 ms (LFS shas) | cross-repo byte-identity ⇒ proof experts were preserved verbatim. **Caveat: re-sharding defeats whole-file sha comparison entirely** — `openai/gpt-oss-120b` ships the same weights twice with all 22 `lfs.sha256` values distinct (§14.1a precondition 2). A sha mismatch therefore does not disprove identity; range-hashing is the fallback, which is what the `quant-eval` skill already advises and this is the concrete reason why. |

### 14.4 Phrasing rules (frontmatter standard)

- **Tier 0 emits observations, not verdicts.** `bpw 4.36` vs `declared 8-bit` is the *good*
  hybrid quant — phrase as `quant: declared 8-bit · effective 4.36 bpw · mixed precision`,
  never as a warning.
- **Bloat is only meaningful relative to the base's native format.** Without lineage, steer
  instead of accusing: `note: 8.5 bpw — at or above 8-bit storage; upcast bloat if the base is
  FP4/INT4-native (enable quant_audit to confirm)`.
- `warning` reserved for hard dead-ends: gated/401, `trust_remote_code` required, a
  quantization block declaring no `bits`.
- Detector *names* stay out of frontmatter (standard §"Audience and phrasing") — say what the
  driver should do, not `fp_grid` or `_classify_pair`.
- **Say nothing about `bpw` when a precondition fails.** Each failure has its own honest line, and
  none of them is a number: un-unpacked `total` (precondition 1) omits `bpw` or reports it against
  the base's count with that substitution stated; multiple checkpoint sets (2) reports the
  canonical set and names the duplicate; pipeline shape (2) omits `bpw` entirely; unquantized (3)
  reports `bpw` as a bare fact with no bloat commentary, or omits it; no weights (4) says `bpw` is
  undefined for a GGUF-only repo. A suppressed number is a correct output, not a gap to fill.

### 14.5 Proposed parameter

`quant_audit: bool = False` on the `model` action.
- **Off (default):** Tier 0 only — free, always emitted, no added latency.
- **On:** adds Tier 1 (base native format + sibling comparison) and Tier 2 (`config.json`
  per-module map, `index.json` component presence) ⇒ ~220–280 ms for a full grid-preservation
  verdict. Tier 3 stays manual via recipe hints.

Auto-enable heuristic (optional): if the repo passes precondition 3's is-a-quant disjunction
**and** `baseModels` is present, Tier 1 alone (+55 ms) is cheap enough to run by default; keep
Tier 2 behind the flag. Reuse gate 3 here rather than writing a second quant test, so the two
cannot drift apart.

### 14.6 Example — bad quant caught for free (Tier 0 only)

```
---
repo: inferencerlabs/MiMo-V2.5-LM-MLX-Q9
model_type: mimo_v2
params: 308.8B
size: 323.6 GiB
quant: undeclared — quantization block declares no bits; effective 9.00 bpw (8-bit + BF16 scale/bias at gs32)
dtype_fingerprint: U32 77.2B + BF16 19.4B          # no E8M0 scales -> all-affine
base_model: XiaomiMiMo/MiMo-V2.5
warning: quantization_config declares a quantization block with no bits — stock loaders cannot construct quantized layers from this config
warning: config declares auto_map (configuration_mimo_v2.MiMoV2Config) — loading requires trust_remote_code=True
note: 9.00 bpw is at or above 8-bit storage; if the base is FP4/INT4-native this is upcast bloat — enable quant_audit to compare against the base's native format
see_also: base XiaomiMiMo/MiMo-V2.5 for native format; sibling quants via search author-scoped on "MiMo-V2.5"
---
```

Why each line here is free and survives the preconditions of §14.1a: `bpw 9.00` passes gate 1
(ratio 3.20, so `total` is genuinely unpacked), gate 2 (one checkpoint set, 36 shards, single
index), gate 3 on two arms (`relation == "quantized"` and `U32` present), and gate 4 (safetensors
block present, non-zero bytes). The first `warning` derives from `'quantization_config' in config`
being true with an empty value, per §14.1b — it is a Tier 0 verdict, not a Tier 2 escalation. The
second replaces an earlier `MODEL_REMAPPING` claim that Parkour cannot make (detector #8).
