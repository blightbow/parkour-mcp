"""HuggingFace Hub integration for model metadata, files, and quant analysis.

Provides a standalone HuggingFace tool with model metadata, repo file reads,
directory listings, and model search — plus the quant-quality analysis that
turns the Hub's free metadata into a judgement about whether a quantized
release is trustworthy.

Design spec: ``docs/huggingface-tool.md``.  The section references in this
module (§14.1a and friends) point there.

Authentication is optional.  A token unlocks gated and private repos and
raises the rate limit; without one the Hub still answers for public models.

Uses httpx directly, mirroring ``github.py``.  Like every other fast path in
this codebase it does not route through ``common.py#guarded_fetch`` — see the
*Outbound fetch hardening* entry in ``.claude/TECH_DEBT.md``.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

import httpx
from pydantic import Field

from ._pipeline import _page_cache
from .common import _API_USER_AGENT, RateLimiter, load_credential, tool_name
from .detection import _detect_hf_url, is_hf_commit_sha
from .markdown import (
    _TRUST_ADVISORY,
    FMEntries,
    _apply_semantic_truncation,
    _build_frontmatter,
    _fence_content,
    _plaintext_presplit,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HF_CONFIG_PATH = Path.home() / ".config" / "parkour" / "hf_token"

_HF_API_BASE = "https://huggingface.co/api"
_HF_SITE_BASE = "https://huggingface.co"

# 1 req/s politeness floor.  The Hub's own ceiling is far higher (500 per
# 300 s fixed window, read from the response headers below); this limiter
# exists so a burst of fast-path calls does not spike a shared bucket.
_hf_limiter = RateLimiter(1.0)

_MAX_RETRIES = 3
_RETRY_BACKOFF = 1.0

_hf_token_cache: str | None = None

# The flagship model call.  ``expand`` is repeatable and coexists with
# ``blobs=true``; with blobs, siblings carry both ``size`` and ``lfs.sha256``,
# which is what makes the whole quant analysis a single round-trip.
#
# The Hub validates this list server-side and a bad token 400s with the full
# valid set in the error body, so ``_hf_request`` surfaces that body verbatim
# rather than swallowing it — the endpoint self-documents its own drift.
_FLAGSHIP_EXPANDS = (
    "safetensors", "config", "gated", "private", "disabled", "cardData",
    "baseModels", "childrenModelCount", "siblings", "tags", "library_name",
    "gguf", "sha", "lastModified", "downloads",
)

_VALID_ACTIONS = ("model", "file", "tree", "search", "org")


def _get_hf_token() -> str:
    """Load the HF token from env var, config file, or return empty string.

    Cached after the first call — the token does not change during a session.
    """
    global _hf_token_cache
    if _hf_token_cache is not None:
        return _hf_token_cache
    _hf_token_cache = load_credential("HF_TOKEN", HF_CONFIG_PATH)
    return _hf_token_cache


def _hf_headers(accept: str = "application/json") -> dict:
    """Build request headers with optional bearer auth."""
    headers = {"User-Agent": _API_USER_AGENT, "Accept": accept}
    if token := _get_hf_token():
        headers["Authorization"] = f"Bearer {token}"
    return headers


# ---------------------------------------------------------------------------
# Rate limit tracking
# ---------------------------------------------------------------------------
# The Hub does not send GitHub's X-RateLimit-* headers.  It sends RFC 9651
# structured fields, where the parameters trail a quoted bucket name:
#
#     ratelimit: "api";r=495;t=140
#     ratelimit-policy: "fixed window";"api";q=500;w=300
#
# so `int(header)` is not the parse.  r = requests remaining, t = seconds
# until the window resets, q = quota, w = window length.

_SF_PARAM_RE = re.compile(r";\s*([a-z]+)\s*=\s*(\d+)", re.IGNORECASE)
_SF_BUCKET_RE = re.compile(r'"([^"]+)"')


@dataclass
class _HFRateLimit:
    """Parsed rate-limit state from the Hub's structured-field headers."""
    bucket: str = "api"
    remaining: int | None = None
    reset_seconds: int | None = None
    quota: int | None = None
    window: int | None = None

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> "_HFRateLimit | None":
        raw = headers.get("ratelimit")
        if not raw:
            return None
        params = {k.lower(): int(v) for k, v in _SF_PARAM_RE.findall(raw)}
        bucket_match = _SF_BUCKET_RE.search(raw)
        policy = headers.get("ratelimit-policy", "")
        policy_params = {
            k.lower(): int(v) for k, v in _SF_PARAM_RE.findall(policy)
        }
        return cls(
            bucket=bucket_match.group(1) if bucket_match else "api",
            remaining=params.get("r"),
            reset_seconds=params.get("t"),
            quota=policy_params.get("q"),
            window=policy_params.get("w"),
        )


_rate_limits: dict[str, _HFRateLimit] = {}

# Warn only once the bucket is genuinely close to empty.  The Hub's window is
# 500/300s, so a research session never approaches it and a chatty warning
# would be noise on every single call.
_RL_WARN_THRESHOLD = 25


def _hf_rate_limit_warning() -> str | None:
    """Return a warning when a rate-limit bucket is nearly exhausted."""
    for rl in _rate_limits.values():
        if rl.remaining is not None and rl.remaining < _RL_WARN_THRESHOLD:
            reset = f", resets in {rl.reset_seconds}s" if rl.reset_seconds else ""
            quota = f" of {rl.quota}" if rl.quota else ""
            hint = "" if _get_hf_token() else " Set HF_TOKEN to raise the limit."
            return (
                f"HuggingFace rate limit low: {rl.remaining}{quota} requests "
                f"remaining on the '{rl.bucket}' bucket{reset}.{hint}"
            )
    return None


def _reset_hf_state() -> None:
    """Clear session caches. Test seam; not called in normal operation."""
    global _hf_token_cache
    _hf_token_cache = None
    _rate_limits.clear()


# ---------------------------------------------------------------------------
# Core HTTP request
# ---------------------------------------------------------------------------

# The Hub returns a byte-identical 401 for gated-and-invisible, private, and
# nonexistent repos when unauthenticated.  Guessing between them would be a
# confident lie, so the error names all three.
_AMBIGUOUS_401 = (
    "Error: HTTP 401 from the Hub for '{repo}'. The repo is gated, private, "
    "or does not exist — the Hub returns an identical 401 for all three when "
    "unauthenticated. Set HF_TOKEN to disambiguate."
)


async def _hf_request(
    path: str,
    params: dict | None = None,
    *,
    repo: str = "",
    base: str = _HF_API_BASE,
) -> Any:
    """Core HTTP call to the Hub API.

    Returns parsed JSON on success, or an error string on failure.  Retries
    5xx with constant backoff.  Records rate-limit state from the structured
    fields on every response.
    """
    url = f"{base}{path}" if path.startswith("/") else path

    for attempt in range(_MAX_RETRIES + 1):
        await _hf_limiter.wait()

        try:
            async with httpx.AsyncClient(
                timeout=30.0, follow_redirects=True,
            ) as client:
                response = await client.get(
                    url, headers=_hf_headers(), params=params,
                )
        except httpx.TimeoutException:
            return "Error: HuggingFace API request timed out."
        except httpx.RequestError as e:
            return f"Error: HuggingFace API request failed - {type(e).__name__}"

        if rl := _HFRateLimit.from_headers(response.headers):
            _rate_limits[rl.bucket] = rl

        if response.status_code == 200:
            try:
                return response.json()
            except Exception:
                logger.debug("HuggingFace response for %s was not JSON", path, exc_info=True)
                return response.text

        if response.status_code == 401:
            return _AMBIGUOUS_401.format(repo=repo or path.lstrip("/"))

        if response.status_code == 403:
            return (
                f"Error: HTTP 403 from the Hub for '{repo or path}'. Access is "
                f"forbidden — for a gated repo this means access has not been "
                f"granted to this token yet."
            )

        if response.status_code == 404:
            return f"Error: Not found on the Hub: '{repo or path}'."

        if response.status_code == 400:
            # The Hub enumerates valid values in the body (notably the full
            # `expand` token list). Surfacing it verbatim keeps the endpoint
            # self-documenting instead of hiding a drift signal.
            try:
                detail = response.json().get("error", "")
            except Exception:
                logger.debug("HuggingFace 400 body for %s was not JSON", path, exc_info=True)
                detail = response.text[:400]
            return f"Error: HuggingFace API rejected the request — {detail}"

        if response.status_code == 429:
            rl = _rate_limits.get("api")
            reset = f" Retry in {rl.reset_seconds}s." if rl and rl.reset_seconds else ""
            return f"Error: Rate limited by the Hub.{reset}"

        if response.status_code >= 500:
            if attempt < _MAX_RETRIES:
                logger.info(
                    "HuggingFace %d on %s, retry %d after %.1fs",
                    response.status_code, path, attempt + 1, _RETRY_BACKOFF,
                )
                await asyncio.sleep(_RETRY_BACKOFF)
                continue
            return f"Error: HuggingFace API returned HTTP {response.status_code}."

        return f"Error: HuggingFace API returned HTTP {response.status_code}."

    return "Error: HuggingFace API request failed."


# ---------------------------------------------------------------------------
# Field shape validation
# ---------------------------------------------------------------------------
# Frontmatter is the trusted zone.  Only structured, enum-shaped, or numeric
# fields may be promoted into it; uploader free text (card prose, tag strings,
# cardData descriptions) stays inside the content fence.  These validators are
# the promotion gate — a field that fails its shape check is dropped from
# frontmatter rather than sanitized, because a mangled value in the trusted
# zone is worse than an absent one.

_REPO_PATH_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,95}/[\w.-]{1,96}$")
_IDENT_RE = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")


def _safe_repo_path(value: Any) -> str | None:
    """Return *value* when it is shaped like an ``org/name`` repo path."""
    if isinstance(value, str) and _REPO_PATH_RE.match(value):
        return value
    return None


def _safe_identifier(value: Any) -> str | None:
    """Return *value* when it is a short registry-style identifier."""
    if isinstance(value, str) and _IDENT_RE.match(value):
        return value
    return None


# ---------------------------------------------------------------------------
# Quant analysis — §14.1a preconditions
# ---------------------------------------------------------------------------

# Filenames of the form "<stem>-00003-of-00014.safetensors".  The group count
# (N) identifies the checkpoint set; the index does not, because indexing is
# not consistently 0- or 1-based across publishers (openai/gpt-oss-120b is
# 0-indexed, mlx-community/deepseek-ai-DeepSeek-V4-Flash-8bit is 1-indexed).
# So "file count == N" is never a valid duplicate test; "more than one distinct
# of-N group" is.
_SHARD_RE = re.compile(r"-(\d+)-of-(\d+)\.safetensors$")

# A ".fp16"/".fp32"/".bf16" infix marks a precision variant of a sibling file,
# a same-directory duplicate class distinct from `consolidated`.
_PRECISION_RE = re.compile(r"\.(fp16|fp32|bf16)\.safetensors$", re.IGNORECASE)

# Container dtypes: packing formats, never logical weight dtypes.  U32 is the
# strongest of the three because nothing stores real weights in it.
_CONTAINER_DTYPES = ("U32", "U8", "I8")

_SHARD_INDEX = "model.safetensors.index.json"
_PIPELINE_INDEX = "model_index.json"


@dataclass
class _CheckpointSet:
    """One candidate set of ``.safetensors`` files within a repo."""
    directory: str
    group: str
    files: list[dict] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(f.get("size") or 0 for f in self.files)

    @property
    def label(self) -> str:
        where = self.directory or "top-level"
        if self.group == "singles":
            if len(self.files) == 1:
                return f"{where}/{self.files[0]['rfilename'].rsplit('/', 1)[-1]}"
            return f"{where} ({len(self.files)} unsharded files)"
        return f"{where} ({len(self.files)} files, {self.group})"


def _partition_checkpoint_sets(siblings: list[dict]) -> list[_CheckpointSet]:
    """Partition ``.safetensors`` siblings into candidate checkpoint sets.

    Keyed on ``(directory, group)`` where *group* is the ``-of-N`` shard group
    when the filename carries one and the literal bucket ``"singles"``
    otherwise.

    The ``"singles"`` bucket is load-bearing, not cosmetic.  Keying each
    unsharded file under its own name over-partitions: ``XiaomiMiMo/MiMo-V2.5``
    ships 18 top-level ``.safetensors`` with no ``-of-N`` anywhere
    (``model_pp0_ep4_shard0.safetensors`` and friends), so per-filename keys
    make every file its own "checkpoint", the canonical pick lands on a 1.11
    GiB fragment, and bpw reports 0.03 against a true 8.13.  That repo passes
    the diffusers pipeline gate cleanly, so nothing else catches it.
    """
    grouped: dict[tuple[str, str], _CheckpointSet] = {}
    for sibling in siblings:
        name = sibling.get("rfilename", "")
        if not name.endswith(".safetensors"):
            continue
        directory = name.rsplit("/", 1)[0] if "/" in name else ""
        match = _SHARD_RE.search(name)
        group = f"of-{int(match.group(2))}" if match else "singles"
        key = (directory, group)
        if key not in grouped:
            grouped[key] = _CheckpointSet(directory=directory, group=group)
        grouped[key].files.append(sibling)
    return list(grouped.values())


def _collapse_precision_variants(sets: list[_CheckpointSet]) -> list[_CheckpointSet]:
    """Drop precision-variant files whose full-precision stem is present.

    ``stabilityai/stable-diffusion-xl-base-1.0`` carries
    ``unet/diffusion_pytorch_model.fp16.safetensors`` (4.78 GiB) beside
    ``unet/diffusion_pytorch_model.safetensors`` (9.56 GiB), a clean 2:1.
    Counting both double-counts one component.
    """
    stems = {
        s.files[i]["rfilename"]
        for s in sets for i in range(len(s.files))
    }
    for candidate in sets:
        candidate.files = [
            f for f in candidate.files
            if not (
                _PRECISION_RE.search(f["rfilename"])
                and _PRECISION_RE.sub(".safetensors", f["rfilename"]) in stems
            )
        ]
    return [s for s in sets if s.files]


def _pick_canonical_set(sets: list[_CheckpointSet]) -> _CheckpointSet | None:
    """Choose the set that constitutes *the* checkpoint.

    Prefer a top-level set over one in a subdirectory, prefer a sharded set
    over a bucket of unsharded files, then prefer the one with more files.

    Prefer-top-level means a genuine component living in a subdirectory (both
    MiMo repos carry ``audio_tokenizer/model.safetensors``, 0.61 GiB) is
    excluded from canonical bytes.  That is accepted: the error is sub-1% and
    it *under*-counts bytes, which biases bpw downward and therefore away from
    falsely accusing a repo of bloat.  For a detector whose failure mode is
    libel, a bounded under-count is the right direction to err.
    """
    if not sets:
        return None
    return min(sets, key=lambda s: (s.directory != "", s.group == "singles", -len(s.files)))


@dataclass
class _QuantReport:
    """Everything the Tier 0 detectors derived from the flagship response."""
    total_params: int | None = None
    dtypes: dict[str, int] = field(default_factory=dict)
    canonical_bytes: int = 0
    canonical_label: str = ""
    canonical_files: list[str] = field(default_factory=list)
    duplicate_labels: list[str] = field(default_factory=list)
    all_safetensors_bytes: int = 0

    bpw: float | None = None
    bpw_suppressed: str | None = None

    is_quant: bool = False
    declared_bits: int | None = None
    quant_method: str | None = None
    quant_config_present: bool = False
    quant_config_empty: bool = False

    fp_grid: float | None = None
    native_format: str | None = None
    pipeline_shape: bool = False
    sharded: bool = False
    shard_count: int = 0
    has_index: bool = False
    auto_map: str | None = None


def _is_quant(
    dtypes: dict[str, int],
    base_relation: str | None,
    tags: list[str],
    qc_present: bool,
    qc: dict,
) -> bool:
    """Is this repo actually a quantization? (§14.1a precondition 3)

    Computed correctly, ``bpw`` on a stock unquantized release equals its
    storage width by definition, so the bloat detector would fire on every
    BF16 model on the Hub.  Unquantized repos outnumber quants, which makes
    that the highest-frequency wrong output available.

    The disjunction is load-bearing.  Collapsing it to "non-empty
    ``quantization_config``" suppresses exactly ``inferencerlabs/
    MiMo-V2.5-LM-MLX-Q9``, the motivating case, whose projected config is
    ``{}``.  *Widening* it with that arm is safe where collapsing is not: a
    disjunction only ever admits more, and a repo that declares a non-empty
    quantization block has self-identified.  That fourth arm is what catches
    natively-quantized releases like ``XiaomiMiMo/MiMo-V2.5``
    (``{"quant_method": "fp8"}``, no container dtype, no quant tag, no
    lineage), which the original three arms all miss.
    """
    if base_relation == "quantized":
        return True
    if any(d in dtypes for d in _CONTAINER_DTYPES):
        return True
    if any(
        t.lower().startswith(("quantized", "gguf", "awq", "gptq", "mlx"))
        or re.match(r"^\d+-?bit$", t.lower())
        for t in tags
    ):
        return True
    return bool(qc_present and qc)


def _classify_native_format(dtypes: dict[str, int]) -> str | None:
    """Name this repo's own weight format from its dtype fingerprint.

    A mixed release names both grids: ``deepseek-ai/DeepSeek-V4-Flash`` runs
    FP4 experts (I8 bulk + E8M0 scales) over an FP8 backbone, and reporting
    only the larger half would misdescribe what a consumer has to load.
    """
    if "F8_E8M0" in dtypes and "I8" in dtypes:
        base = "FP4-packed (E8M0 scales)"
        return f"{base} + FP8 (E4M3)" if "F8_E4M3" in dtypes else base
    if "F8_E4M3" in dtypes and dtypes.get("F8_E4M3", 0) > sum(
        v for k, v in dtypes.items() if k != "F8_E4M3"
    ):
        return "FP8-native (E4M3)"
    if "U32" in dtypes and "U8" in dtypes:
        return "affine + E8M0 scales"
    if "U32" in dtypes:
        return "affine"
    if "U8" in dtypes and "U32" not in dtypes:
        return "byte-packed (mxfp-style)"
    return None


def analyze_quant(payload: dict) -> _QuantReport:
    """Derive every Tier 0 quant signal from the flagship response.

    Applies the four mandatory preconditions of §14.1a before emitting a
    ``bpw``.  Each precondition failure sets ``bpw_suppressed`` to an honest
    explanation instead of a number: a suppressed number is a correct output,
    not a gap to fill.
    """
    report = _QuantReport()

    safetensors = payload.get("safetensors")
    dtypes = {
        k: v for k, v in ((safetensors or {}).get("parameters") or {}).items()
        if isinstance(v, int)
    }
    report.dtypes = dtypes
    report.total_params = (safetensors or {}).get("total")

    siblings = payload.get("siblings") or []
    names = [s.get("rfilename", "") for s in siblings]
    report.has_index = _SHARD_INDEX in names
    report.pipeline_shape = _PIPELINE_INDEX in names and not report.has_index

    config = payload.get("config") or {}
    # Presence, never truthiness — see §14.1b. A present-but-emptied dict is
    # a distinct signal from an absent key, and `.get(k, {})` erases it.
    report.quant_config_present = "quantization_config" in config
    qc = config.get("quantization_config") if report.quant_config_present else {}
    qc = qc if isinstance(qc, dict) else {}
    report.quant_config_empty = report.quant_config_present and not qc
    bits = qc.get("bits")
    report.declared_bits = bits if isinstance(bits, int) else None
    report.quant_method = _safe_identifier(qc.get("quant_method"))

    auto_map = config.get("auto_map")
    if isinstance(auto_map, dict) and (cfg := auto_map.get("AutoConfig")):
        report.auto_map = str(cfg)[:120]

    base_models = payload.get("baseModels")
    base_relation = (
        base_models.get("relation") if isinstance(base_models, dict) else None
    )
    tags = [t for t in (payload.get("tags") or []) if isinstance(t, str)]
    report.is_quant = _is_quant(
        dtypes, base_relation, tags, report.quant_config_present, qc,
    )

    # fp_grid: share of scale metadata carried as E8M0 rather than BF16/F16.
    #
    # E8M0 scales land in two different buckets depending on who wrote the
    # checkpoint: MLX packs them into a bare `U8` array, while a natively
    # microscaled release reports a real `F8_E8M0` dtype. Counting only `U8`
    # scores `deepseek-ai/DeepSeek-V4-Flash` at 0.0 and calls a preserved FP
    # grid "all-affine", which is the opposite of the truth.
    e8m0 = dtypes.get("U8", 0) + dtypes.get("F8_E8M0", 0)
    scale_like = e8m0 + dtypes.get("BF16", 0) + dtypes.get("F16", 0)
    if scale_like:
        report.fp_grid = e8m0 / scale_like
    report.native_format = _classify_native_format(dtypes)

    sets = _collapse_precision_variants(_partition_checkpoint_sets(siblings))
    report.all_safetensors_bytes = sum(s.bytes for s in sets)
    canonical = _pick_canonical_set(sets)
    if canonical:
        report.canonical_bytes = canonical.bytes
        report.canonical_label = canonical.label
        report.canonical_files = sorted(
            f["rfilename"] for f in canonical.files
        )
        report.duplicate_labels = [
            s.label for s in sets if s is not canonical
        ]
        report.sharded = canonical.group != "singles"
        report.shard_count = len(canonical.files)

    report.bpw_suppressed = _suppress_reason(report, dtypes)
    if report.bpw_suppressed is None and report.total_params:
        report.bpw = report.canonical_bytes * 8 / report.total_params
        # Cross-check against the declared width. `bits` survives the config
        # whitelist whenever it is present, so this closes the residual hole
        # in the U32 test: a repo packed into a container dtype that the Hub
        # did not unpack lands at a bpw wildly above its declared width.
        if report.declared_bits and report.bpw > 2 * report.declared_bits:
            report.bpw = None
            report.bpw_suppressed = (
                f"effective bits-per-weight ({report.canonical_bytes * 8 / report.total_params:.2f}) "
                f"is more than double the declared {report.declared_bits}-bit width, "
                f"so the Hub's parameter count is not a logical weight count here"
            )
    return report


def _suppress_reason(report: _QuantReport, dtypes: dict[str, int]) -> str | None:
    """Return why ``bpw`` must not be emitted, or None when all gates pass."""
    # Precondition 4 — a repo may have nothing to divide. GGUF-only repos are
    # common and report relation="quantized", so precondition 3 passes them
    # straight into a division by a None denominator.
    if report.total_params is None:
        return (
            "the repo publishes no safetensors metadata, so there is no "
            "parameter count to divide by"
        )
    if not report.canonical_bytes:
        return "no .safetensors weights are present to measure"

    # Pipeline shape — safetensors.total counts one component while bytes
    # count the whole pipeline, so no set choice yields a meaningful ratio.
    if report.pipeline_shape:
        return (
            "this is a diffusers pipeline (model_index.json with no top-level "
            "safetensors index), where the parameter count covers one component "
            "and the bytes cover every component"
        )

    # Precondition 1 — U32 present with total == Sigma means the Hub did not
    # unpack sub-byte weights, so `total` is storage elements, not logical
    # weights. Exact equality, not a tolerance band: un-unpacked repos land at
    # exactly 1.0 and the minimum real packing ratio is 2:1, so the margin is
    # enormous and a band only invites a tuning bug.
    sigma = sum(dtypes.values())
    if sigma and "U32" in dtypes and report.total_params == sigma:
        return (
            "the Hub reported packed storage elements rather than logical "
            "weights for this repo (U32 container present and the parameter "
            "total equals the sum of stored elements)"
        )
    return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _humanize_params(n: int | None) -> str | None:
    """Render a parameter count as a compact human string."""
    if not n:
        return None
    if n >= 1e9:
        return f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    return f"{n:,}"


def _humanize_bytes(n: int | None) -> str | None:
    """Render a byte count in GiB / MiB."""
    if not n:
        return None
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GiB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MiB"
    return f"{n:,} B"


def _format_dtype_fingerprint(dtypes: dict[str, int], limit: int = 4) -> str | None:
    """Render the by-dtype counts as a compact quant signature.

    Unconditionally safe to print — unlike ``bpw``, these are raw counts with
    no ratio to get wrong.

    Buckets under 0.5% of the total are dropped.  Real checkpoints carry a
    handful of tiny housekeeping arrays (a 12k-element F32 bucket, a 2k I64
    bucket) that say nothing about the quantization and crowd out the two or
    three dtypes that do.
    """
    if not dtypes:
        return None
    sigma = sum(dtypes.values())
    if not sigma:
        return None
    material = {k: v for k, v in dtypes.items() if v / sigma >= 0.005}
    ordered = sorted((material or dtypes).items(), key=lambda kv: -kv[1])[:limit]
    return " + ".join(f"{k} {_humanize_params(v)}" for k, v in ordered)


def _format_quant_summary(report: _QuantReport) -> str | None:
    """Summarize the quantization for the ``quant:`` frontmatter field."""
    parts = []
    if report.quant_method:
        parts.append(report.quant_method)
    if report.declared_bits:
        parts.append(f"declared {report.declared_bits}-bit")
    elif report.quant_config_empty:
        parts.append("undeclared — quantization block declares no bits")
    if report.bpw is not None:
        parts.append(f"effective {report.bpw:.2f} bpw")
        if report.declared_bits and report.bpw < report.declared_bits - 0.5:
            parts.append("mixed precision")
    if not parts and report.native_format:
        parts.append(report.native_format)
    return " · ".join(parts) if parts else None


def _fm_base(source: str) -> FMEntries:
    """Build common frontmatter entries.

    Seeds ``trust`` because every action here fences uploader-controlled
    content (model cards, filenames, tag strings).
    """
    entries = FMEntries(
        {"source": source, "api": "HuggingFace", "trust": _TRUST_ADVISORY},
    )
    entries.append("warning", _hf_rate_limit_warning())
    return entries


def _repo_url(repo: str, rev: str = "main") -> str:
    if rev and rev != "main":
        return f"{_HF_SITE_BASE}/{repo}/tree/{rev}"
    return f"{_HF_SITE_BASE}/{repo}"


# ---------------------------------------------------------------------------
# Page cache population
# ---------------------------------------------------------------------------

def _cache_page(
    cache_url: str | None,
    title: str,
    content: str,
    *,
    repo: str,
    rev: str,
    presplit: list[tuple[int, str]] | None = None,
) -> None:
    """Cache full content so ``search=`` / ``slices=`` / ``section=`` work.

    The invariant this exists to uphold, borrowed from
    ``_pipeline.py#_github_fast_path``: cache the **whole** document while
    returning a truncated view of it.  Without this the tool truncates a long
    model card, tells the caller to narrow with ``section=``, and then has
    nothing to narrow — the follow-up re-enters the fast path, finds no cache
    entry, and returns the same truncated text.  A steering hint that loops
    back to itself is worse than no hint.

    Entries are tagged ``hf:<repo>@<rev>`` so a repo's card and its config /
    index files evict as a unit rather than leaving orphans behind.

    *presplit* is passed through to ``_page_cache.store``.  Leaving it None
    routes markdown through ``_safe_markdown_presplit``, which carries its own
    circuit breaker for pathological single-line input; structured text should
    supply ``_blob_presplit`` output instead, and skip caching when that
    returns None.
    """
    if not cache_url or not content.strip():
        return
    _page_cache.store(
        cache_url, title, content,
        renderer="huggingface",
        presplit=presplit,
        group=f"hf:{repo}@{rev}",
    )


# ---------------------------------------------------------------------------
# Steering: hints, warnings, and notes
# ---------------------------------------------------------------------------

def _apply_gating_warning(fm: FMEntries, payload: dict, repo: str) -> None:
    """Warn on a gated / private / disabled repo before the 401 happens.

    ``gated`` is a tri-state string (``false`` / ``"auto"`` / ``"manual"``),
    not a bool.  The distinction is the actionable part: ``auto`` is an
    instant click-through, ``manual`` is a human approval queue measured in
    hours to days.  Rendering the enum beats coercing it, and ``gated is True``
    would silently never fire.
    """
    gated = payload.get("gated")
    if isinstance(gated, str) and gated:
        kind = "manual approval" if gated == "manual" else "instant click-through"
        fm.append(
            "warning",
            f"gated ({kind}) — file reads and /resolve/ return 401 without "
            f"granted access; request at {_HF_SITE_BASE}/{repo} or set HF_TOKEN",
        )
    if payload.get("private"):
        fm.append("warning", "private repo — visible only to authorized tokens")
    if payload.get("disabled"):
        fm.append("warning", "this repo is disabled on the Hub")


def _apply_quant_steering(
    fm: FMEntries, report: _QuantReport, base_model: str | None,
) -> None:
    """Attach the Tier 0 quant observations, verdicts, and steering.

    Phrasing follows §14.4: Tier 0 emits observations, not verdicts.  A
    ``warning`` is reserved for hard dead-ends (a quantization block that
    declares no bits, remote code required); anything about bloat is a
    conditional ``note`` because bloat is only meaningful relative to the
    base's native format.
    """
    if report.quant_config_empty:
        fm.append(
            "warning",
            "quantization_config declares a quantization block with no bits — "
            "stock loaders cannot construct quantized layers from this config",
        )
    if report.auto_map:
        fm.append(
            "warning",
            f"config declares auto_map ({report.auto_map}) — loading requires "
            f"trust_remote_code=True",
        )

    if report.duplicate_labels:
        # Naming the naive total is the point of the note, not decoration: it
        # is the number a caller would have reached by summing every
        # .safetensors themselves, and the gap between the two is the whole
        # reason the canonical set has to be picked rather than assumed.
        fm.append(
            "note",
            f"repo ships more than one checkpoint set; sizes and "
            f"bits-per-weight are measured against {report.canonical_label}, "
            f"ignoring {', '.join(report.duplicate_labels)} — summing every "
            f".safetensors instead would report "
            f"{_humanize_bytes(report.all_safetensors_bytes)}",
        )

    if report.bpw_suppressed:
        fm.append(
            "note",
            f"effective bits-per-weight not reported: {report.bpw_suppressed}",
        )
        if report.pipeline_shape:
            return

    # Bloat commentary is gated on actually being a quant. On a stock BF16
    # release bpw equals the storage width by definition, so this note on an
    # unquantized repo would be arithmetically true and completely astonishing.
    if report.bpw is not None and report.is_quant and report.bpw >= 8.4:
        if base_model:
            fm.append(
                "note",
                f"{report.bpw:.2f} bpw is at or above 8-bit storage; if "
                f"{base_model} is FP4/INT4-native this is upcast bloat — set "
                f"quant_audit=true to compare against the base's native format",
            )
        else:
            fm.append(
                "note",
                f"{report.bpw:.2f} bpw is at or above 8-bit storage; upcast "
                f"bloat if the base is FP4/INT4-native, but this repo declares "
                f"no lineage so there is nothing to compare against",
            )

    if report.is_quant and report.fp_grid is not None:
        if report.fp_grid > 0.5:
            fm.append(
                "note",
                "native FP-microscaling grid preserved (E8M0 scales present)",
            )
        elif report.native_format == "affine":
            fm.append(
                "note",
                "all-affine — no E8M0 scales present, so a native FP grid on "
                "the base was regridded rather than preserved",
            )


# Quant markers publishers append to a family name. Stripped repeatedly, not
# once: "MiMo-V2.5-LM-MLX-Q9" carries three of them stacked, and stopping
# after the first leaves a search string that only finds the repo you already
# have instead of the family you wanted to compare it against.
_QUANT_SUFFIX_RE = re.compile(
    r"[-_.](\d+bit|\d+-bit|q\d+(?:_[kms0-9]+)*|oq\d+|gguf|mlx|awq|gptq|"
    r"exl\d*|fp\d+|int\d+|bf16|f16|mxfp\d+|hf|lm|instruct-\d+bit)$",
    re.IGNORECASE,
)


def _family_stem(repo: str) -> str:
    """Reduce a quant repo name to the family a sibling search should target."""
    stem = repo.split("/")[-1]
    while True:
        reduced = _QUANT_SUFFIX_RE.sub("", stem)
        if reduced == stem or not reduced:
            return stem
        stem = reduced


def _apply_model_hints(
    fm: FMEntries,
    repo: str,
    rev: str,
    report: _QuantReport,
    base_model: str | None,
    weight_file: str | None,
    filenames: set[str],
) -> None:
    """Attach the follow-up steering that pre-answers the next fetch.

    Every suggested file is checked against the repo's actual file list first.
    A hint naming a file the repo does not ship spends the caller's next tool
    call on a 404, which is worse than staying quiet.
    """
    hf = tool_name("huggingface")
    targets = [
        name for name in ("config.json", _SHARD_INDEX) if name in filenames
    ]
    if targets:
        listed = " or ".join(f"'{repo}/{t}'" for t in targets)
        fm.append(
            "hint",
            f"{hf}(action=file, query={listed}) for the full document — the "
            f"free config summary omits mode and group_size",
        )

    if weight_file:
        fm.append(
            "hint",
            f"a .safetensors header is range-readable without pulling the "
            f"shard: GET {_HF_SITE_BASE}/{repo}/resolve/{rev}/{weight_file} "
            f"with 'Range: bytes=0-7' for the little-endian u64 header length, "
            f"then bytes 8..8+len for the per-tensor dtype/shape JSON",
        )

    if base_model:
        fm.append(
            "see_also",
            f"base model {base_model} — fetch it for the native weight format; "
            f"an FP4/FP8/INT4-native base changes the quant calculus",
        )

    if report.is_quant:
        family = _family_stem(repo)
        fm.append(
            "see_also",
            f"sibling quants of this family: {hf}(action=search, "
            f"query='{family}') to compare effective bits-per-weight across "
            f"the family",
        )


# ---------------------------------------------------------------------------
# Action: model
# ---------------------------------------------------------------------------

async def _fetch_model_card(repo: str, rev: str) -> str | None:
    """Fetch README.md for the model card body, or None.

    Runs concurrently with the flagship metadata call, so the card costs
    wall-clock latency only when it is slower than the metadata request.
    """
    result = await _hf_request(
        f"/{repo}/raw/{rev}/README.md", repo=repo, base=_HF_SITE_BASE,
    )
    if isinstance(result, str) and not result.startswith("Error"):
        return result
    return None


_CARD_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
_CARD_BASE_MODEL_RE = re.compile(
    r"^base_model:\s*\n?\s*-?\s*([\w.-]+/[\w.-]+)", re.MULTILINE,
)


def _strip_card_frontmatter(card: str) -> str:
    """Remove the card's own YAML block from the rendered body.

    The card's frontmatter is uploader-controlled text that would otherwise
    land directly above our own trusted ``---`` block inside the fence, which
    reads as though the two were the same document.
    """
    return _CARD_FRONTMATTER_RE.sub("", card, count=1)


def _base_model_from_payload(payload: dict, card: str | None) -> str | None:
    """Resolve the base model from structured lineage, then the card.

    ``baseModels`` is a single object carrying one ``relation`` plus a
    ``models`` list — not a list of per-model relations — and it is ``null``
    rather than ``[]`` or absent when a repo declares no lineage.
    """
    base_models = payload.get("baseModels")
    if isinstance(base_models, dict):
        models = base_models.get("models") or []
        if (
            models and isinstance(models[0], dict)
            and (resolved := _safe_repo_path(models[0].get("id")))
        ):
            return resolved

    card_data = payload.get("cardData") or {}
    declared = card_data.get("base_model")
    if isinstance(declared, list):
        declared = declared[0] if declared else None
    if resolved := _safe_repo_path(declared):
        return resolved

    # Last resort: the card body's own `base_model:` line. Without this the
    # agent has to re-fetch the README to answer a question the tool already
    # had the bytes for.
    if card and (m := _CARD_BASE_MODEL_RE.search(card)):
        return _safe_repo_path(m.group(1))
    return None


def _base_relation(payload: dict) -> str | None:
    base_models = payload.get("baseModels")
    if isinstance(base_models, dict):
        relation = base_models.get("relation")
        return relation if isinstance(relation, str) else None
    return None


async def _action_model(
    query: str, quant_audit: bool = False, cache_url: str | None = None,
) -> str:
    """Fetch model metadata, the quant analysis, and the model card."""
    repo, rev = _split_repo_rev(query)
    if not repo:
        return (
            f"Error: Could not parse '{query}' as a model repo. "
            f"Expected 'org/name' or a huggingface.co model URL."
        )

    params = {"blobs": "true", "expand": list(_FLAGSHIP_EXPANDS)}
    payload, card = await asyncio.gather(
        _hf_request(f"/models/{repo}", params=params, repo=repo),
        _fetch_model_card(repo, rev),
    )
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return f"Error: Unexpected response shape from the Hub for '{repo}'."

    report = analyze_quant(payload)
    base_model = _base_model_from_payload(payload, card)
    config = payload.get("config") or {}

    fm = _fm_base(_repo_url(repo, rev))
    fm["repo"] = repo

    if model_type := _safe_identifier(config.get("model_type")):
        fm["model_type"] = model_type
    architectures = config.get("architectures") or []
    if architectures and (arch := _safe_identifier(architectures[0])):
        fm["architecture"] = arch

    fm["params"] = _humanize_params(report.total_params)
    if report.canonical_bytes:
        fm["size"] = _humanize_bytes(report.canonical_bytes)
    if report.shard_count:
        fm["weights"] = (
            f"sharded — {report.shard_count} shards, "
            f"{_SHARD_INDEX} {'present' if report.has_index else 'absent'}"
            if report.sharded
            else f"{report.shard_count} unsharded file(s)"
        )
    fm["quant"] = _format_quant_summary(report)
    fm["dtype_fingerprint"] = _format_dtype_fingerprint(report.dtypes)
    if report.native_format:
        fm["weight_format"] = report.native_format

    if library := _safe_identifier(payload.get("library_name")):
        fm["library"] = library
    # Render the enum, never a bool. The Hub's own values are `false`,
    # `"auto"`, and `"manual"`, and the distinction is the actionable part:
    # auto is an instant click-through, manual is a human approval queue
    # measured in hours to days. Coercing to a Python bool also leaks a
    # capitalized `False` into a YAML block that should read `false`.
    gated = payload.get("gated")
    fm["gated"] = gated if isinstance(gated, str) and gated else "false"
    if payload.get("private"):
        fm["private"] = True
    if payload.get("disabled"):
        fm["disabled"] = True
    fm["base_model"] = base_model
    if relation := _base_relation(payload):
        fm["lineage_relation"] = _safe_identifier(relation)
    if sha := payload.get("sha"):
        fm["revision"] = str(sha)[:40]
        if is_hf_commit_sha(rev):
            fm["revision_pinned"] = True
    if isinstance(payload.get("downloads"), int):
        fm["downloads"] = payload["downloads"]
    if last_modified := payload.get("lastModified"):
        fm["last_modified"] = str(last_modified)[:10]

    gguf = payload.get("gguf")
    if isinstance(gguf, dict) and (total := gguf.get("total")):
        fm["gguf_params"] = _humanize_params(total)

    _apply_gating_warning(fm, payload, repo)
    _apply_quant_steering(fm, report, base_model)

    siblings = payload.get("siblings") or []
    filenames = {s["rfilename"] for s in siblings if s.get("rfilename")}
    # Point the range-read recipe at a shard of the *canonical* set. Picking
    # the first .safetensors in the repo would aim it at whatever sorts first,
    # which on a repo with an auxiliary component is a 0.6 GiB audio tokenizer
    # rather than a shard of the model the caller asked about.
    weight_file = report.canonical_files[0] if report.canonical_files else None
    _apply_model_hints(
        fm, repo, rev, report, base_model, weight_file, filenames,
    )

    if quant_audit:
        audit = await _run_quant_audit(report, base_model)
        for line in audit:
            fm.append("note", line)

    body_parts = []
    if tags := [t for t in (payload.get("tags") or []) if isinstance(t, str)]:
        body_parts.append(f"**Tags:** {', '.join(tags[:30])}\n")
    if card:
        stripped = _strip_card_frontmatter(card)
        _cache_page(cache_url, repo, stripped, repo=repo, rev=rev)
        truncated, trunc_hint = _apply_semantic_truncation(stripped, 2000)
        body_parts.append(truncated)
        if trunc_hint and cache_url:
            fm.append(
                "hint",
                f"model card truncated — the full card is cached, so "
                f"{tool_name('web_fetch_direct')}('{cache_url}', section=...) "
                f"or search=/slices= narrows it without another fetch",
            )
        elif trunc_hint:
            fm.append(
                "hint",
                f"model card truncated — "
                f"{tool_name('web_fetch_direct')}"
                f"('{_HF_SITE_BASE}/{repo}', section=...) for specific sections",
            )
    else:
        body_parts.append("_No model card (README.md) in this repo._")

    return (
        _build_frontmatter(fm) + "\n\n"
        + _fence_content("\n".join(body_parts), title=repo)
    )


async def _run_quant_audit(
    report: _QuantReport, base_model: str | None,
) -> list[str]:
    """Tier 1 — fetch the base model's own format for a grid verdict.

    One extra round-trip.  Buys the grid-preservation verdict and, when
    precondition 1 suppressed ``bpw``, a trustworthy parameter count to
    compute it against.
    """
    if not base_model:
        return [
            ("quant_audit requested but this repo declares no base model, "
            "so there is no native format to compare against"),
        ]
    params = {"blobs": "true", "expand": list(_FLAGSHIP_EXPANDS)}
    payload = await _hf_request(
        f"/models/{base_model}", params=params, repo=base_model,
    )
    if isinstance(payload, str):
        return [f"quant_audit could not read base {base_model}: {payload}"]
    if not isinstance(payload, dict):
        return [f"quant_audit got an unexpected response for {base_model}"]

    base = analyze_quant(payload)
    lines = []
    if base.native_format:
        lines.append(f"base {base_model} weight format: {base.native_format}")

    # Precondition 1's stated remedy: substitute the base's parameter count,
    # and say that a substitution happened rather than passing the number off
    # as this repo's own.
    if report.bpw is None and base.total_params and report.canonical_bytes:
        substituted = report.canonical_bytes * 8 / base.total_params
        lines.append(
            f"effective {substituted:.2f} bpw when measured against the base's "
            f"parameter count ({_humanize_params(base.total_params)}), "
            f"substituted because this repo's own count is packed storage "
            f"elements rather than logical weights",
        )

    if report.bpw is not None and base.bpw is not None:
        lines.append(
            f"base measures {base.bpw:.2f} bpw against this repo's "
            f"{report.bpw:.2f} bpw",
        )

    children = payload.get("childrenModelCount")
    # A dict keyed by relation, not an integer — `> 0` raises TypeError and a
    # bare truthiness test is unconditionally true. It also counts the *base's*
    # children, which is exactly the sibling set worth comparing against.
    if isinstance(children, dict) and (quantized := children.get("quantized")):
        lines.append(
            f"{quantized} quantizations of {base_model} exist on the Hub — "
            f"{tool_name('huggingface')}(action=search, "
            f"query='{base_model.split('/')[-1]}') to compare them",
        )
    return lines


# ---------------------------------------------------------------------------
# Query parsing
# ---------------------------------------------------------------------------

def _split_repo_rev(query: str) -> tuple[str, str]:
    """Parse a query into ``(repo, revision)``.

    Accepts ``org/name``, ``org/name@rev``, or any huggingface.co URL.
    """
    query = query.strip()
    if match := _detect_hf_url(query):
        if match.kind in ("dataset", "space"):
            return "", "main"
        return match.repo, match.sha or match.rev
    if "@" in query:
        repo, _, rev = query.partition("@")
        return (repo.strip() if _safe_repo_path(repo.strip()) else ""), rev.strip()
    return (query if _safe_repo_path(query) else ""), "main"


def _split_repo_path(query: str) -> tuple[str, str, str]:
    """Parse ``org/name/path/to/file`` into ``(repo, path, revision)``."""
    query = query.strip()
    if match := _detect_hf_url(query):
        if match.kind in ("dataset", "space"):
            return "", "", "main"
        return match.repo, match.path or "", match.sha or match.rev

    rev = "main"
    if "@" in query:
        query, _, rev = query.partition("@")
        query, rev = query.strip(), rev.strip()
    segments = [s for s in query.split("/") if s]
    if len(segments) < 3:
        return "", "", rev
    return f"{segments[0]}/{segments[1]}", "/".join(segments[2:]), rev


# ---------------------------------------------------------------------------
# Action: file
# ---------------------------------------------------------------------------

# Weight payloads are never transferred. A single shard runs to multiple GiB
# and the useful content (per-tensor dtype and shape) sits in a header the
# caller can range-read for a few KiB.
_WEIGHT_EXTS = (".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".gguf", ".onnx")

# Raw file reads are capped well below the shard threshold; a config or
# tokenizer file that exceeds this is pathological.
_MAX_FILE_BYTES = 1_000_000

_GGUF_QUANT_RE = re.compile(
    r"[.-](IQ\d+_\w+|Q\d+_[KMS0-9_]+|Q\d+|F16|F32|BF16)\.gguf$", re.IGNORECASE,
)


def _sibling_index(payload: dict) -> dict[str, dict]:
    return {
        s["rfilename"]: s
        for s in (payload.get("siblings") or [])
        if s.get("rfilename")
    }


async def _action_file(
    query: str, ref: str | None = None, cache_url: str | None = None,
) -> str:
    """Fetch a repo file, or describe it when fetching would be wasteful."""
    repo, path, rev = _split_repo_path(query)
    if not repo or not path:
        return (
            f"Error: Could not parse '{query}' as a repo file. Expected "
            f"'org/name/path/to/file' or a huggingface.co blob/resolve URL."
        )
    rev = ref or rev

    if path.endswith(_WEIGHT_EXTS):
        return await _describe_weight_file(repo, path, rev)

    result = await _hf_request(
        f"/{repo}/raw/{rev}/{path}", repo=repo, base=_HF_SITE_BASE,
    )
    if isinstance(result, str) and result.startswith("Error"):
        return result

    text = result if isinstance(result, str) else None
    parsed = result if isinstance(result, (dict, list)) else None
    if text is None and parsed is None:
        return f"Error: Unexpected response for '{repo}/{path}'."

    # Render first, then size-gate the rendered form. Gating only the `text`
    # branch would exempt every JSON file from the cap, and a repo's
    # weight_map is exactly the kind of JSON that runs to tens of MB.
    # Pretty-print JSON before caching rather than after. A minified
    # config.json is one enormous line, which trips the presplit circuit
    # breaker and makes the file unsliceable; indenting gives it the line
    # structure the line-oriented presplit needs.
    rendered = json.dumps(parsed, indent=2) if parsed is not None else (text or "")

    if len(rendered.encode("utf-8", "ignore")) > _MAX_FILE_BYTES:
        return (
            f"Error: '{path}' renders to more than {_MAX_FILE_BYTES:,} bytes. "
            f"Fetch it directly at {_HF_SITE_BASE}/{repo}/resolve/{rev}/{path}."
        )

    fm = _fm_base(f"{_HF_SITE_BASE}/{repo}/blob/{rev}/{path}")
    fm["repo"] = repo
    fm["path"] = path
    fm["revision"] = rev
    if is_hf_commit_sha(rev):
        fm["revision_pinned"] = True

    basename = path.rsplit("/", 1)[-1]
    if basename == "config.json" and isinstance(parsed, dict):
        _apply_config_frontmatter(fm, parsed)
    elif basename.endswith("index.json") and isinstance(parsed, dict):
        _apply_index_frontmatter(fm, parsed, basename)

    if parsed is not None:
        body = f"```json\n{rendered}\n```"
    else:
        lang = "markdown" if basename.endswith(".md") else ""
        body = f"```{lang}\n{rendered}\n```" if rendered else ""

    _cache_file_body(cache_url, repo, path, rev, basename, rendered)

    truncated, trunc_hint = _apply_semantic_truncation(body, 6000)
    if trunc_hint:
        fm.append("hint", trunc_hint)
        if cache_url:
            fm.append(
                "hint",
                "the full file is cached — search= or slices= narrows it "
                "without another fetch",
            )
    return (
        _build_frontmatter(fm) + "\n\n"
        + _fence_content(truncated, title=f"{repo}/{path}")
    )


def _cache_file_body(
    cache_url: str | None,
    repo: str,
    path: str,
    rev: str,
    basename: str,
    rendered: str,
) -> None:
    """Cache a repo file's full text for downstream slicing.

    Markdown gets the semantic splitter; everything else goes through
    ``github.py#_blob_presplit``, which tries a tree-sitter grammar first and
    falls back to line chunks.  When that returns None the content is
    pathological (a single line over 1 MB, issue #6) and caching is skipped
    entirely — the caller still gets formatted output, only slicing is lost.
    """
    if not cache_url or not rendered.strip():
        return
    if basename.endswith(".md"):
        _cache_page(cache_url, path, rendered, repo=repo, rev=rev)
        return
    presplit = _plaintext_presplit(rendered)
    if presplit is not None:
        _cache_page(
            cache_url, path, rendered, repo=repo, rev=rev, presplit=presplit,
        )


def _apply_config_frontmatter(fm: FMEntries, config: dict) -> None:
    """Frontload the fields a caller reads config.json for."""
    if model_type := _safe_identifier(config.get("model_type")):
        fm["model_type"] = model_type
    architectures = config.get("architectures") or []
    if architectures and (arch := _safe_identifier(architectures[0])):
        fm["architecture"] = arch
    for key in ("max_position_embeddings", "num_hidden_layers",
                "hidden_size", "num_attention_heads", "vocab_size"):
        if isinstance(config.get(key), int):
            fm[key] = config[key]
    if isinstance(config.get("num_experts"), int):
        fm["num_experts"] = config["num_experts"]
    if isinstance(config.get("num_experts_per_tok"), int):
        fm["experts_per_token"] = config["num_experts_per_tok"]

    # Full config.json carries mode and group_size, which the free `config`
    # expand drops — that is the whole reason to spend this call.
    if "quantization_config" in config:
        qc = config.get("quantization_config") or {}
        if isinstance(qc, dict):
            summary = ", ".join(
                f"{k}={v}" for k, v in qc.items()
                if isinstance(v, (str, int, float, bool))
            )
            fm["quantization"] = summary or "declared, no scalar fields"
    if config.get("auto_map"):
        fm.append(
            "warning",
            "config declares auto_map — loading requires trust_remote_code=True",
        )


def _apply_index_frontmatter(
    fm: FMEntries, index: dict, basename: str,
) -> None:
    """Frontload shard-map statistics from a ``*.index.json``.

    ``model_index.json`` is a diffusers *pipeline* manifest and
    ``model.safetensors.index.json`` is a shard map.  The names near-collide
    and mean different things, so say which one this is.
    """
    if basename == _PIPELINE_INDEX:
        fm["index_kind"] = "diffusers pipeline manifest (not a shard map)"
        components = [k for k in index if not k.startswith("_")]
        fm["components"] = len(components)
        return

    fm["index_kind"] = "safetensors shard map"
    weight_map = index.get("weight_map") or {}
    if isinstance(weight_map, dict):
        fm["tensor_keys"] = len(weight_map)
        fm["shards"] = len(set(weight_map.values()))
    metadata = index.get("metadata") or {}
    if isinstance(metadata, dict) and isinstance(metadata.get("total_size"), int):
        fm["declared_total_size"] = _humanize_bytes(metadata["total_size"])
    fm.append(
        "hint",
        "weight_map keys name every tensor and the shard holding it — "
        "search= for component prefixes (mtp, vision, audio) to detect a "
        "component the quantizer silently stripped",
    )


async def _describe_weight_file(repo: str, path: str, rev: str) -> str:
    """Describe a weight file without transferring it.

    Returns size, LFS sha256, and the exact range-read recipe.  A shard is
    multiple GiB and the caller almost always wants the header, not the
    tensors.
    """
    params = {"blobs": "true", "expand": ["siblings", "gguf", "gated"]}
    payload = await _hf_request(f"/models/{repo}", params=params, repo=repo)
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return f"Error: Unexpected response shape from the Hub for '{repo}'."

    sibling = _sibling_index(payload).get(path)
    resolve_url = f"{_HF_SITE_BASE}/{repo}/resolve/{rev}/{path}"

    fm = _fm_base(f"{_HF_SITE_BASE}/{repo}/blob/{rev}/{path}")
    fm["repo"] = repo
    fm["path"] = path
    fm["revision"] = rev
    fm["content"] = "not transferred — weight payload"

    if sibling is None:
        fm.append(
            "warning",
            f"'{path}' is not in this repo's file list at revision {rev}; "
            f"the size and checksum below are unavailable",
        )
    else:
        fm["size"] = _humanize_bytes(sibling.get("size"))
        if (sha := (sibling.get("lfs") or {}).get("sha256")):
            fm["lfs_sha256"] = sha

    _apply_gating_warning(fm, payload, repo)

    if path.endswith(".gguf"):
        if m := _GGUF_QUANT_RE.search(path):
            fm["gguf_quant"] = m.group(1).upper()
        gguf = payload.get("gguf")
        if isinstance(gguf, dict):
            if total := gguf.get("total"):
                fm["gguf_params"] = _humanize_params(total)
            if arch := _safe_identifier(gguf.get("architecture")):
                fm["gguf_architecture"] = arch
        body = (
            f"GGUF weight file. Quant type is encoded in the filename; the "
            f"Hub's own GGUF metadata is frontloaded above.\n\n"
            f"Download URL (multi-GB, not fetched):\n{resolve_url}"
        )
    else:
        fm.append(
            "hint",
            f"read the header without the payload: GET {resolve_url} with "
            f"'Range: bytes=0-7' gives a little-endian u64 header length N; "
            f"'Range: bytes=8-{{8+N-1}}' gives the JSON header mapping every "
            f"tensor to its dtype, shape, and byte offsets",
        )
        body = (
            f"Safetensors weight file — payload deliberately not transferred.\n\n"
            f"Header layout: the first 8 bytes are a little-endian u64 giving "
            f"the JSON header length N. Bytes 8..8+N hold the header itself, "
            f"a JSON object mapping each tensor name to `dtype`, `shape`, and "
            f"`data_offsets`. That is typically a few hundred KiB against a "
            f"multi-GB file.\n\n"
            f"Download URL (not fetched):\n{resolve_url}"
        )
    return (
        _build_frontmatter(fm) + "\n\n"
        + _fence_content(body, title=f"{repo}/{path}")
    )


# ---------------------------------------------------------------------------
# Action: tree
# ---------------------------------------------------------------------------

async def _action_tree(query: str, ref: str | None = None) -> str:
    """List a repo's files with sizes and LFS checksums."""
    repo, path, rev = _split_repo_path(query)
    if not repo:
        repo, rev = _split_repo_rev(query)
        path = ""
    if not repo:
        return (
            f"Error: Could not parse '{query}' as a repo. Expected 'org/name' "
            f"or 'org/name/subdirectory'."
        )
    rev = ref or rev

    params = {"blobs": "true", "expand": ["siblings", "gated", "sha"]}
    payload = await _hf_request(f"/models/{repo}", params=params, repo=repo)
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return f"Error: Unexpected response shape from the Hub for '{repo}'."

    siblings = payload.get("siblings") or []
    prefix = f"{path.rstrip('/')}/" if path else ""
    entries = [
        s for s in siblings
        if s.get("rfilename", "").startswith(prefix)
    ]
    if not entries:
        return (
            f"No files under '{prefix or '/'}' in {repo} at revision {rev}. "
            f"The repo has {len(siblings)} files in total."
        )

    fm = _fm_base(f"{_HF_SITE_BASE}/{repo}/tree/{rev}/{path}".rstrip("/"))
    fm["repo"] = repo
    fm["revision"] = rev
    fm["files"] = len(entries)
    fm["total_size"] = _humanize_bytes(
        sum(s.get("size") or 0 for s in entries),
    )
    _apply_gating_warning(fm, payload, repo)

    sets = _partition_checkpoint_sets(siblings)
    if len(sets) > 1:
        fm.append(
            "note",
            f"repo ships {len(sets)} distinct .safetensors sets "
            f"({', '.join(s.label for s in sets)}) — they are not all one "
            f"checkpoint, so summing every shard over-counts the model",
        )

    rows = []
    for sibling in sorted(entries, key=lambda s: s["rfilename"]):
        name = sibling["rfilename"]
        size = _humanize_bytes(sibling.get("size")) or "—"
        sha = (sibling.get("lfs") or {}).get("sha256")
        suffix = f"  lfs:{sha[:12]}" if sha else ""
        rows.append(f"{size:>12}  {name}{suffix}")

    body = "\n".join(rows)
    truncated, trunc_hint = _apply_semantic_truncation(body, 4000)
    if trunc_hint:
        fm.append("hint", trunc_hint)
    return (
        _build_frontmatter(fm) + "\n\n"
        + _fence_content(truncated, title=f"{repo} @ {rev}")
    )


# ---------------------------------------------------------------------------
# Actions: search and org
# ---------------------------------------------------------------------------

_SORT_FIELDS = ("downloads", "likes", "lastModified", "trendingScore")


async def _action_search(
    query: str,
    limit: int = 10,
    author: str | None = None,
    sort: str = "downloads",
) -> str:
    """Search Hub models by free text, optionally scoped to an author."""
    if sort not in _SORT_FIELDS:
        return (
            f"Error: Unknown sort '{sort}'. "
            f"Valid values: {', '.join(_SORT_FIELDS)}"
        )
    params: dict[str, Any] = {
        "limit": max(1, min(limit, 100)),
        "sort": sort,
        "direction": -1,
        "expand": ["downloads", "likes", "lastModified", "library_name",
                   "gated", "tags", "pipeline_tag"],
    }
    if query.strip():
        params["search"] = query.strip()
    if author:
        params["author"] = author

    payload = await _hf_request("/models", params=params)
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, list):
        return "Error: Unexpected response shape from the Hub search."

    label = f"search '{query}'" if query.strip() else "models"
    if author:
        label += f" by {author}"
    fm = _fm_base(f"{_HF_SITE_BASE}/models")
    fm["query"] = query.strip() or None
    fm["author"] = author
    fm["sort"] = sort
    fm["results"] = len(payload)
    if not payload:
        fm.append(
            "hint",
            "no matches — Hub search is substring-based over repo ids, so a "
            "shorter fragment of the family name usually finds more",
        )

    rows = []
    for item in payload:
        repo_id = item.get("id") or item.get("modelId") or "?"
        downloads = item.get("downloads")
        likes = item.get("likes")
        library = item.get("library_name") or "—"
        gated = item.get("gated")
        stats = []
        if isinstance(downloads, int):
            stats.append(f"{downloads:,} downloads")
        if isinstance(likes, int):
            stats.append(f"{likes} likes")
        stats.append(f"library: {library}")
        if isinstance(gated, str) and gated:
            stats.append(f"gated: {gated}")
        rows.append(f"- **{repo_id}** — {' | '.join(stats)}")

    hf = tool_name("huggingface")
    fm.append(
        "hint",
        f"{hf}(action=model, query='<org/name>') for the metadata beta, "
        f"including effective bits-per-weight where it is computable",
    )
    return (
        _build_frontmatter(fm) + "\n\n"
        + _fence_content("\n".join(rows), title=label)
    )


async def _action_org(query: str, limit: int = 20) -> str:
    """List an org or user's models."""
    match = _detect_hf_url(query)
    author = match.org if match else query.strip().strip("/")
    if not author or "/" in author:
        return (
            f"Error: Could not parse '{query}' as an org or user. "
            f"Expected a bare name or a huggingface.co/<org> URL."
        )
    return await _action_search("", limit=limit, author=author)


# ---------------------------------------------------------------------------
# Fast path (called from fetch_direct.py)
# ---------------------------------------------------------------------------

_UNSUPPORTED_REPO_TYPE = (
    "HuggingFace {kind}s are out of scope for this tool, which covers models. "
    "The page is still readable through the generic fetch path."
)


async def _hf_fast_path(url: str) -> str | None:
    """Handle a huggingface.co URL through the API, or return None.

    Returning None means "not mine, fall through to the generic pipeline" —
    the caller must not treat that as an error, and this must not swallow a
    failure silently.
    """
    match = _detect_hf_url(url)
    if match is None:
        return None
    if match.kind in ("dataset", "space"):
        # Recognised and declined. Datasets need the separate datasets-server
        # API and Spaces are an app, not a document; both would be answered
        # wrongly by the model handler.
        return None

    # `cache_url` is the URL as asked, not a canonical rewrite: fetch_direct
    # looks the entry up by exactly the string the caller passed, so a
    # normalized key would leave search=/section= unable to find it.
    try:
        if match.kind == "org":
            return await _action_org(match.org)
        if match.kind == "file" and match.path:
            return await _action_file(
                f"{match.repo}/{match.path}", ref=match.rev, cache_url=url,
            )
        if match.kind == "tree":
            target = (
                f"{match.repo}/{match.path}" if match.path else match.repo
            )
            return await _action_tree(target, ref=match.rev)
        rev = match.sha or match.rev
        return await _action_model(
            f"{match.repo}@{rev}" if rev != "main" else match.repo,
            cache_url=url,
        )
    except Exception:
        logger.warning("HF fast path failed for %s", url, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------

async def huggingface(
    action: Annotated[str, Field(
        description=(
            "The operation to perform. "
            "model: model metadata, quantization analysis, and model card. "
            "file: read a repo file (weight files are described, never downloaded). "
            "tree: list repo files with sizes and LFS checksums. "
            "search: find models by name, optionally scoped to an author. "
            "org: list an organization's or user's models."
        ),
    )],
    query: Annotated[str, Field(
        description=(
            "For model/tree: 'org/name' (optionally 'org/name@revision'). "
            "For file: 'org/name/path/to/file'. "
            "For search: free text (Hub search is substring-based over repo ids). "
            "For org: the organization or user name. "
            "Any huggingface.co URL is also accepted and routed automatically."
        ),
    )],
    ref: Annotated[str | None, Field(
        description="Git revision (branch, tag, or commit SHA) for file/tree. Defaults to main.",
    )] = None,
    limit: Annotated[int, Field(
        description="Maximum results for search/org (default 10, max 100).",
    )] = 10,
    author: Annotated[str | None, Field(
        description="Scope a search to one organization or user.",
    )] = None,
    sort: Annotated[str, Field(
        description="Sort field for search/org: downloads, likes, lastModified, or trendingScore.",
    )] = "downloads",
    quant_audit: Annotated[bool, Field(
        description=(
            "On the model action, spend one extra request to read the base "
            "model's native weight format. Buys the grid-preservation verdict "
            "and a trustworthy parameter count when the Hub reported packed "
            "storage elements instead of logical weights."
        ),
    )] = False,
) -> str:
    """Explore HuggingFace Hub models, files, and quantization quality."""
    action = action.strip().lower()
    if action not in _VALID_ACTIONS:
        return (
            f"Error: Unknown action '{action}'. "
            f"Valid actions: {', '.join(_VALID_ACTIONS)}"
        )

    # A pasted URL routes to the handler its shape implies, so the caller
    # never has to pick an action that contradicts the link they have.
    if match := _detect_hf_url(query.strip()):
        if match.kind in ("dataset", "space"):
            return "Error: " + _UNSUPPORTED_REPO_TYPE.format(kind=match.kind)
        if match.kind == "file" and action in ("model", "tree"):
            action = "file"
        elif match.kind == "tree" and action == "model":
            action = "tree"
        elif match.kind == "org" and action == "model":
            action = "org"

    if action == "model":
        return await _action_model(query, quant_audit=quant_audit)
    if action == "file":
        return await _action_file(query, ref=ref)
    if action == "tree":
        return await _action_tree(query, ref=ref)
    if action == "search":
        return await _action_search(query, limit=limit, author=author, sort=sort)
    return await _action_org(query, limit=limit)
