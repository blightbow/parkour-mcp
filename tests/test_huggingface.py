"""Tests for parkour_mcp.huggingface (mocked, no network).

The quant-analysis tests are the load-bearing ones.  Each ``bpw`` precondition
in ``docs/huggingface-tool.md`` §14.1a has a real public repo that fails *only*
that gate, and the dtype counts, file lists, and sizes in the fixtures below
are transcribed from live Hub responses for those repos.  A detector that
regresses past one of these gates does not merely return a wrong number: it
publishes a false accusation of "bloat" against an honest release, which is
why each gate gets a named test rather than sharing a parametrized one.
"""

import httpx
import pytest
import respx

from parkour_mcp._pipeline import _page_cache
from parkour_mcp.detection import _detect_hf_url, is_hf_commit_sha
from parkour_mcp.huggingface import (
    _apply_config_frontmatter,
    _cache_file_body,
    _checkpoint_format,
    _classify_native_format,
    _declared_grid,
    _family_stem,
    _fm_base,
    _format_dtype_fingerprint,
    _has_affine_module,
    _hf_fast_path,
    _hf_request,
    _HFRateLimit,
    _histogram_is_storage_scoped,
    _partition_checkpoint_sets,
    _pick_canonical_set,
    _quant_block,
    _scale_implied_packing,
    _split_repo_path,
    _split_repo_rev,
    analyze_quant,
    huggingface,
)
from parkour_mcp.markdown import _TRUST_ADVISORY, FMEntries

GIB = 1024 ** 3


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _shards(stem: str, count: int, total_bytes: int, start: int = 1) -> list[dict]:
    """Build a sharded ``-N-of-M`` sibling list totalling *total_bytes*."""
    declared = count - 1 if start == 0 else count
    each = total_bytes // count
    return [
        {
            "rfilename": f"{stem}-{i:05d}-of-{declared:05d}.safetensors",
            "size": each,
            "lfs": {"sha256": f"{i:064x}"},
        }
        for i in range(start, start + count)
    ]


def _payload(**overrides) -> dict:
    base = {
        "id": "org/model",
        "safetensors": {"parameters": {"BF16": 8_000_000_000}, "total": 8_000_000_000},
        "siblings": [{"rfilename": "model.safetensors.index.json", "size": 100}],
        "config": {},
        "tags": [],
        "gated": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# §14.1a precondition 1 — denominator validity (the U32 guard)
# ---------------------------------------------------------------------------

class TestPrecondition1DenominatorValidity:
    """`safetensors.total` is sometimes storage elements, not logical weights."""

    def test_unpacked_repo_reports_bpw(self):
        """A genuine 3.2x packing ratio means `total` is a logical count."""
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {"U32": 77_182_000_000, "BF16": 19_345_000_000},
                "total": 308_780_000_000,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 36, int(323.56 * GIB)),
            ],
        ))
        assert report.bpw is not None
        assert report.bpw == pytest.approx(9.00, abs=0.02)

    def test_u32_with_exact_equality_suppresses_bpw(self):
        """dawncr0w/MiMo-V2.5-oQ4-MLX: ratio exactly 1.0 with a U32 container.

        Without this gate the tool reports ~28.7 bpw on a legitimate 4-bit
        quant and calls it bloat.
        """
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {"U32": 39_628_000_000, "BF16": 10_465_000_000},
                "total": 50_093_000_000,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 30, int(167.12 * GIB)),
            ],
            config={"quantization_config": {"bits": 4}},
        ))
        assert report.bpw is None
        assert report.bpw_suppressed is not None
        assert "packed storage elements" in report.bpw_suppressed

    def test_equality_without_u32_is_legitimate(self):
        """openai/gpt-oss-120b also reports total == sum, but unpacked into U8.

        Ratio 1.0 alone is not the signal; ratio 1.0 *with U32* is. Keying on
        the ratio alone would suppress a repo whose number is perfectly good.
        """
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {"U8": 118_245_000_000, "BF16": 2_167_000_000},
                "total": 120_412_000_000,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 15, int(60.77 * GIB), start=0),
            ],
            config={"quantization_config": {"quant_method": "mxfp4"}},
        ))
        assert report.bpw == pytest.approx(4.34, abs=0.02)

    def test_bits_cross_check_suppresses_impossible_ratio(self):
        """A bpw far above the declared width means the denominator is wrong.

        Closes the residual hole in the U32 test: a container dtype the Hub
        failed to unpack that is not U32 would otherwise pass gate 1.
        """
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {"U8": 40_000_000_000, "BF16": 10_000_000_000},
                "total": 50_000_000_000,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 10, int(160 * GIB)),
            ],
            config={"quantization_config": {"bits": 4}},
        ))
        assert report.bpw is None
        assert report.bpw_suppressed is not None
        assert "double the declared 4-bit" in report.bpw_suppressed

    def test_config_groups_supplies_the_declared_width(self):
        """nvidia/Qwen3.6-35B-A3B-NVFP4: no top-level `bits`, so no ceiling.

        compressed-tensors and modelopt nest widths under
        `config_groups[*].weights.num_bits`, which survives the Hub's config
        whitelist. Without reading it `declared_bits` is None, the
        declared-width cross-check has no ceiling, and this 4-bit release
        measures 10.03 bpw against a true ~5.4 and draws the `bpw >= 8.4`
        upcast-bloat note against its own vendor.

        The minimum width is the right ceiling: tested against group_0's 8,
        the bogus 10.03 passes.
        """
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {
                    "BF16": 1_825_916_784,
                    "F8_E4M3": 3_332_177_920,
                    "U8": 16_423_321_600,
                },
                "total": 18_683_860_336,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 3, int(21.81 * GIB)),
            ],
            config={"quantization_config": {
                "quant_method": "modelopt",
                "config_groups": {
                    "group_0": {"weights": {"num_bits": 8}},
                    "group_1": {"weights": {"num_bits": 4}},
                },
            }},
        ))
        assert report.declared_bits == 4
        assert report.bpw is None
        assert report.bpw_suppressed is not None
        assert "double the declared 4-bit" in report.bpw_suppressed

    def test_i8_nibble_packing_suppresses_bpw(self):
        """deepseek-ai/DeepSeek-V4-Flash: FP4 packed two-per-I8, no U32.

        The repo §14.1a called theoretical.  It defeats the U32 key (no U32
        anywhere) *and* the declared-width cross-check (it declares
        ``quant_method`` with no ``bits``), landing at ratio exactly 1.0000.
        Unguarded it reports 8.08 bpw against a true ~4.39, which renders a
        native-FP4 vendor release as an 8-bit upload and inverts every requant
        verdict drawn from comparing a quant against it.

        The scale array is what settles it with no declared width in hand:
        8.86B E8M0 scales over a 141.7B-element I8 payload is 2.0001 weights
        per stored byte.
        """
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {
                    "BF16": 1_415_259_264,
                    "I64": 2_327_040,
                    "F32": 36_168_018,
                    "F8_E8M0": 8_858_737_664,
                    "F8_E4M3": 6_023_020_544,
                    "I8": 141_733_920_768,
                },
                "total": 158_069_433_298,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 46, int(148.66 * GIB)),
            ],
            config={"quantization_config": {"quant_method": "fp8"}},
        ))
        assert report.bpw is None
        assert report.bpw_suppressed is not None
        assert "packed storage elements" in report.bpw_suppressed
        assert "2.0 weights per stored byte" in report.bpw_suppressed

    def test_unpacked_i8_without_scale_bucket_reports_bpw(self):
        """deepseek-ai/DeepSeek-V4-Flash-0731: same family, Hub *did* unpack.

        The sibling of the case above, and the reason the new gate measures
        packing rather than keying on ``I8``.  Here the Hub unpacked the FP4
        into logical weight counts and dropped the E8M0 scales from the
        histogram entirely, so ``total`` is a true weight count and 4.39 bpw
        is correct.  With no scale bucket the measurement abstains, which is
        what lets a correct number through.
        """
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {
                    "BF16": 1_483_567_488,
                    "I64": 2_327_040,
                    "F32": 37_741_630,
                    "F8_E4M3": 6_304_038_912,
                    "I8": 296_352_743_424,
                },
                "total": 304_180_418_494,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 48, int(155.43 * GIB)),
            ],
            config={"quantization_config": {"quant_method": "fp8"}},
        ))
        assert report.bpw == pytest.approx(4.39, abs=0.02)

    def test_byte_per_weight_microscaling_is_not_packed(self):
        """An mxfp8 payload stores one weight per byte: packing 1.0, not 2.0.

        Guards the threshold from the other side.  A microscaled checkpoint
        with a distinct E8M0 bucket is exactly the shape the new gate inspects,
        so it must not suppress one whose payload is *not* sub-byte packed.
        """
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {"I8": 32_000_000_000, "F8_E8M0": 1_000_000_000},
                "total": 33_000_000_000,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 8, int(30.73 * GIB)),
            ],
            config={"quantization_config": {"quant_method": "mxfp8"}},
        ))
        assert _scale_implied_packing(report.dtypes) == pytest.approx(1.0)
        assert report.bpw is not None
        assert report.bpw_suppressed is None


# ---------------------------------------------------------------------------
# §14.1a precondition 2 — numerator validity (checkpoint-set partition)
# ---------------------------------------------------------------------------

def _split_weights(stems: int, per_stem_bytes: int) -> list[dict]:
    """Individually split weights: many stems, each cut `-00001-of-00002`."""
    return [
        {
            "rfilename": f"model_{i}_linear_fc1-{p:05d}-of-00002.safetensors",
            "size": per_stem_bytes // 2,
        }
        for i in range(stems) for p in (1, 2)
    ]


class TestPrecondition2CheckpointSets:
    """Summing every `.safetensors` over-counts repos with duplicate sets."""

    def test_multi_stem_of_n_is_not_a_rival_set(self):
        """XiaomiMiMo/MiMo-V2-Flash-Base: `-of-N` under 47 distinct stems.

        Those are individual weights that each needed splitting, not a global
        shard index, so they are siblings of the directory's unsharded files.
        Treating them as a competing set discards the other 111 GB, which
        understates bpw by 36% and leaves the histogram reconciling against a
        set covering 65% of what it counts.
        """
        siblings = [
            *[{"rfilename": f"model_{i}.safetensors", "size": 10 ** 9}
              for i in range(98)],
            *_split_weights(47, 2 * 10 ** 9),
        ]
        sets = _partition_checkpoint_sets(siblings)
        assert len(sets) == 1
        assert len(sets[0].files) == 192
        assert "per-file weights" in sets[0].label

    def test_single_stem_of_n_stays_a_rival_set(self):
        """mistralai ships consolidated.safetensors beside `of-10` shards.

        One stem across the group means `-of-N` is a real shard index, so the
        unsharded blob remains a duplicate rather than being folded in.
        """
        siblings = [
            {"rfilename": "consolidated.safetensors", "size": 48 * 10 ** 9},
            *_shards("model", 10, 48 * 10 ** 9),
        ]
        sets = _partition_checkpoint_sets(siblings)
        assert {s.group for s in sets} == {"singles", "of-10"}

    def test_split_weights_stay_within_their_directory(self):
        """Folding must not reach across directories and merge a duplicate."""
        siblings = [
            *[{"rfilename": f"model_{i}.safetensors", "size": 10 ** 9}
              for i in range(4)],
            *[{"rfilename": f"original/{f['rfilename']}", "size": f["size"]}
              for f in _split_weights(3, 2 * 10 ** 9)],
        ]
        sets = _partition_checkpoint_sets(siblings)
        assert {s.directory for s in sets} == {"", "original"}

    def test_resharded_duplicate_in_subdirectory(self):
        """openai/gpt-oss-120b ships the same weights re-sharded under original/.

        All 22 checksums differ, so sha-based dedup collapses nothing; the
        partition is what separates the two sets.
        """
        siblings = [
            {"rfilename": "model.safetensors.index.json", "size": 100},
            {"rfilename": "original/model.safetensors.index.json", "size": 100},
            *_shards("model", 15, int(60.77 * GIB), start=0),
            *_shards("original/model", 7, int(60.77 * GIB)),
        ]
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {"U8": 118_245_000_000, "BF16": 2_167_000_000},
                "total": 120_412_000_000,
            },
            siblings=siblings,
        ))
        assert report.bpw == pytest.approx(4.34, abs=0.02)
        assert report.all_safetensors_bytes > report.canonical_bytes
        assert report.duplicate_labels

    def test_consolidated_blob_beside_sharded_set(self):
        """mistralai/* ships consolidated.safetensors at top level.

        "Top-level only" is not a fix here — both sets are top-level, so the
        sharded-over-single preference is what picks correctly.
        """
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {"BF16": 24_011_000_000},
                "total": 24_011_000_000,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 10, int(44.72 * GIB)),
                {"rfilename": "consolidated.safetensors", "size": int(44.72 * GIB)},
            ],
        ))
        assert report.bpw == pytest.approx(16.00, abs=0.02)

    def test_unsharded_top_level_files_form_one_set(self):
        """XiaomiMiMo/MiMo-V2.5 ships 18 top-level files with no -of-N anywhere.

        This is the regression that the "singles" bucket exists for: keyed per
        filename, every file becomes its own checkpoint, the canonical pick
        lands on a 1.11 GiB fragment, and bpw reports 0.03 against a true 8.13.
        The repo passes the diffusers pipeline gate cleanly, so nothing else
        catches it.
        """
        siblings = [
            {"rfilename": "model.safetensors.index.json", "size": 100},
            *[
                {"rfilename": f"model_pp0_ep{i}_shard0.safetensors",
                 "size": int(32.01 * GIB)}
                for i in range(8)
            ],
            *[
                {"rfilename": f"model_pp0_ep{i}_shard1.safetensors",
                 "size": int(3.25 * GIB)}
                for i in range(1, 8)
            ],
            {"rfilename": "model_pp0_ep0_shard1.safetensors", "size": int(13.47 * GIB)},
            {"rfilename": "model_mtp.safetensors", "size": int(1.11 * GIB)},
        ]
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {"F8_E4M3": 306_655_000_000, "BF16": 4_052_000_000},
                "total": 310_775_000_000,
            },
            siblings=siblings,
            config={"quantization_config": {"quant_method": "fp8"}},
        ))
        assert report.bpw is not None, report.bpw_suppressed
        assert report.bpw == pytest.approx(8.1, abs=0.2)

    def test_shard_index_is_not_a_file_count_test(self):
        """0-indexed and 1-indexed publishers both exist, so count != N.

        gpt-oss numbers from 00000 (15 files for "of-14"); most others number
        from 00001. Treating "file count != N" as a duplicate signal would
        misfire on one of the two conventions.
        """
        zero_based = _partition_checkpoint_sets(
            _shards("model", 15, 15_000, start=0),
        )
        one_based = _partition_checkpoint_sets(
            _shards("model", 65, 65_000, start=1),
        )
        assert len(zero_based) == 1
        assert len(one_based) == 1
        assert len(zero_based[0].files) == 15
        assert len(one_based[0].files) == 65

    def test_precision_variants_collapse(self):
        """A .fp16 sibling beside its full-precision stem is a duplicate."""
        sets = _partition_checkpoint_sets([
            {"rfilename": "unet/diffusion_pytorch_model.safetensors",
             "size": int(9.56 * GIB)},
            {"rfilename": "unet/diffusion_pytorch_model.fp16.safetensors",
             "size": int(4.78 * GIB)},
        ])
        report = analyze_quant(_payload(
            safetensors={"parameters": {"F32": 2_567_000_000},
                         "total": 2_567_000_000},
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                {"rfilename": "unet/diffusion_pytorch_model.safetensors",
                 "size": int(9.56 * GIB)},
                {"rfilename": "unet/diffusion_pytorch_model.fp16.safetensors",
                 "size": int(4.78 * GIB)},
            ],
        ))
        assert sum(len(s.files) for s in sets) == 2
        assert report.all_safetensors_bytes == pytest.approx(9.56 * GIB, rel=0.01)

    def test_diffusers_pipeline_suppresses_bpw(self):
        """model_index.json without a top-level shard index is a pipeline.

        `safetensors.total` counts one component while bytes count the whole
        pipeline, so no set choice yields a meaningful ratio.
        """
        report = analyze_quant(_payload(
            safetensors={"parameters": {"BF16": 11_901_000_000},
                         "total": 11_901_000_000},
            siblings=[
                {"rfilename": "model_index.json", "size": 100},
                {"rfilename": "transformer/diffusion_pytorch_model.safetensors",
                 "size": int(22.17 * GIB)},
                {"rfilename": "flux1-dev.safetensors", "size": int(22.17 * GIB)},
            ],
        ))
        assert report.bpw is None
        assert report.bpw_suppressed is not None
        assert "diffusers pipeline" in report.bpw_suppressed

    def test_canonical_pick_prefers_top_level_then_sharded(self):
        sets = _partition_checkpoint_sets([
            {"rfilename": "sub/model-00001-of-00002.safetensors", "size": 10},
            {"rfilename": "sub/model-00002-of-00002.safetensors", "size": 10},
            {"rfilename": "consolidated.safetensors", "size": 40},
            {"rfilename": "model-00001-of-00003.safetensors", "size": 5},
            {"rfilename": "model-00002-of-00003.safetensors", "size": 5},
            {"rfilename": "model-00003-of-00003.safetensors", "size": 5},
        ])
        picked = _pick_canonical_set(sets)
        assert picked is not None
        assert picked.directory == ""
        assert picked.group == "of-3"


# ---------------------------------------------------------------------------
# §14.1a precondition 3 — is this actually a quant
# ---------------------------------------------------------------------------

class TestPrecondition3IsAQuant:
    """Bloat commentary on a stock BF16 release is the highest-frequency
    wrong output available, since unquantized repos outnumber quants."""

    def test_stock_bf16_release_is_not_a_quant(self):
        report = analyze_quant(_payload(
            safetensors={"parameters": {"BF16": 8_292_000_000},
                         "total": 8_292_000_000},
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 5, int(15.45 * GIB)),
            ],
        ))
        assert report.bpw == pytest.approx(16.00, abs=0.02)
        assert report.is_quant is False

    def test_finetune_relation_is_not_a_quant_relation(self):
        """Bloat is meaningless relative to yourself, and a finetune is not
        a quant relationship."""
        report = analyze_quant(_payload(
            safetensors={"parameters": {"BF16": 24_011_000_000},
                         "total": 24_011_000_000},
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 10, int(44.72 * GIB)),
            ],
            baseModels={"relation": "finetune",
                        "models": [{"id": "mistralai/Mistral-Small-3.1-24B-Base-2503"}]},
        ))
        assert report.is_quant is False

    def test_empty_projected_config_still_counts_as_quant(self):
        """The disjunction is load-bearing.

        Collapsing the gate to "non-empty quantization_config" suppresses
        exactly the motivating case, whose projected config is `{}`.
        """
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {"U32": 77_182_000_000, "BF16": 19_345_000_000},
                "total": 308_780_000_000,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 36, int(323.56 * GIB)),
            ],
            config={"quantization_config": {}},
            baseModels={"relation": "quantized",
                        "models": [{"id": "XiaomiMiMo/MiMo-V2.5"}]},
        ))
        assert report.is_quant is True

    def test_self_declared_native_quant_counts(self):
        """A repo declaring {"quant_method": "fp8"} with no container dtype,
        no quant tag, and no lineage is still a quant."""
        report = analyze_quant(_payload(
            safetensors={"parameters": {"F8_E4M3": 306_655_000_000},
                         "total": 306_655_000_000},
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 4, int(280 * GIB)),
            ],
            config={"quantization_config": {"quant_method": "fp8"}},
        ))
        assert report.is_quant is True


# ---------------------------------------------------------------------------
# §14.1a precondition 4 — measurable weights
# ---------------------------------------------------------------------------

class TestPrecondition4MeasurableWeights:
    def test_gguf_only_repo_has_undefined_bpw(self):
        """unsloth/*-GGUF has no safetensors block and relation=quantized, so
        precondition 3 passes it into a None denominator."""
        report = analyze_quant(_payload(
            safetensors=None,
            siblings=[{"rfilename": f"DeepSeek-V3.1-Q4_K_M-{i:05d}.gguf",
                       "size": 40 * GIB} for i in range(3)],
            baseModels={"relation": "quantized",
                        "models": [{"id": "deepseek-ai/DeepSeek-V3.1"}]},
        ))
        assert report.bpw is None
        assert report.bpw_suppressed
        assert "no safetensors metadata" in report.bpw_suppressed

    def test_no_weights_is_not_zero(self):
        report = analyze_quant(_payload(
            safetensors={"parameters": {"BF16": 1000}, "total": 1000},
            siblings=[{"rfilename": "README.md", "size": 10}],
        ))
        assert report.bpw is None
        assert report.bpw != 0


# ---------------------------------------------------------------------------
# §14.1b — presence, never truthiness
# ---------------------------------------------------------------------------

class TestConfigProjection:
    """`expand=config` projects quantization_config through a field whitelist,
    so present-but-emptied is a distinct signal from absent."""

    def test_present_but_empty_is_the_gatekeeping_tell(self):
        report = analyze_quant(_payload(config={"quantization_config": {}}))
        assert report.quant_config_present is True
        assert report.quant_config_empty is True

    def test_absent_key_is_silent(self):
        report = analyze_quant(_payload(config={"model_type": "llama"}))
        assert report.quant_config_present is False
        assert report.quant_config_empty is False

    def test_populated_config_is_neither(self):
        report = analyze_quant(_payload(
            config={"quantization_config": {"bits": 4}},
        ))
        assert report.quant_config_present is True
        assert report.quant_config_empty is False
        assert report.declared_bits == 4

    def test_empty_config_dict_does_not_crash(self):
        """GGUF-only repos project `config` itself to {}."""
        report = analyze_quant(_payload(config={}))
        assert report.quant_config_present is False


# ---------------------------------------------------------------------------
# checkpoint_format — naming the format from config.json alone
# ---------------------------------------------------------------------------

# Transcribed from the live configs of the named repos.
_TRT_BLOCK = {
    "quant_algo": "NVFP4", "kv_cache_quant_algo": None, "group_size": 128,
    "exclude_modules": None, "clamp_val": None, "has_zero_point": False,
    "pre_quant_scale": False, "mamba_ssm_cache_dtype": None,
}
_MLX_BLOCK = {"group_size": 64, "bits": 4, "mode": "affine"}


class TestCheckpointFormat:
    """Every quantizer in the transformers ecosystem self-identifies in
    `quant_method`, and TensorRT-LLM in `quant_algo`.  mlx-lm declares neither,
    so MLX alone is recognised by shape, and the shape it is recognised by has
    a near neighbour that must not be caught."""

    def test_declared_quant_method_wins(self):
        assert _checkpoint_format({
            "quantization_config": {"quant_method": "awq", "bits": 4},
        }) == "awq"

    def test_tensorrt_llm_block_is_never_mlx(self):
        """The trap. `rungalileo/mistral-7b-instruct-v0.3-trtllm-ckpt-bf16`
        and `glux-cz/Qwen3-8B-NVFP4-Blackwell` carry a top-level
        `quantization` dict and no `quantization_config`, which is the shape a
        presence-only MLX rule would claim.  Measured against 483 live configs,
        this is the only family that shape collides with."""
        out = _checkpoint_format({"quantization": _TRT_BLOCK})
        assert out == "TensorRT-LLM (NVFP4)"
        assert "MLX" not in out

    def test_unquantized_tensorrt_llm_matches_on_key_presence(self):
        """A bf16 TRT-LLM checkpoint writes the same block with a null
        `quant_algo`.  Matching the value rather than the key would let it fall
        through to the arms below."""
        block = {**_TRT_BLOCK, "quant_algo": None}
        assert _checkpoint_format({"quantization": block}) == "TensorRT-LLM"

    def test_mirrored_block_is_mlx(self):
        assert _checkpoint_format({
            "quantization": _MLX_BLOCK, "quantization_config": _MLX_BLOCK,
        }) == "MLX (affine)"

    def test_mirror_is_equality_not_co_presence(self):
        """Two differing blocks are not the mlx-lm double-write."""
        assert _checkpoint_format({
            "quantization": {"bits": 4},
            "quantization_config": {"bits": 8},
        }) is None

    def test_empty_blocks_do_not_attribute(self):
        """`{} == {}` is true and says nothing about who wrote it.  Distinct
        from the §14.1b presence test, which asks whether a loader can build
        quantized layers and stays presence-only."""
        assert _checkpoint_format({
            "quantization": {}, "quantization_config": {},
        }) is None

    def test_pre_mirror_mlx_stays_silent(self):
        """2024-vintage mlx-lm (`mlx-community/phi-2-4bit`) wrote
        `quantization` alone, in the same shape TensorRT-LLM uses.  Silence is
        the chosen failure, since the alternative risks the reverse error."""
        assert _checkpoint_format({
            "quantization": {"bits": 4, "group_size": 64},
        }) is None

    def test_unquantized_reports_storage_dtype(self):
        assert _checkpoint_format({"dtype": "bfloat16"}) == "BF16 (unquantized)"

    def test_legacy_torch_dtype_spelling(self):
        assert _checkpoint_format({"torch_dtype": "float16"}) == "FP16 (unquantized)"

    def test_dtype_arm_requires_the_absence_of_a_quant_block(self):
        """On a quantized repo this field is the compute dtype, not the storage
        width, so reporting it would describe the checkpoint wrongly."""
        assert _checkpoint_format({
            "dtype": "bfloat16", "quantization": {"bits": 4, "group_size": 64},
        }) is None


# ---------------------------------------------------------------------------
# config.json frontmatter
# ---------------------------------------------------------------------------

def _fm(config: dict) -> FMEntries:
    entries = FMEntries({})
    _apply_config_frontmatter(entries, config)
    return entries


class TestConfigDimensions:
    """Current-generation releases nest the language-model dimensions under
    `text_config`.  Reading only the top level reported nothing at all for 54
    of 99 sampled popular and recent releases."""

    def test_dimensions_come_from_the_nested_block(self):
        fm = _fm({
            "model_type": "gemma4",
            "text_config": {
                "num_hidden_layers": 42, "hidden_size": 2560,
                "num_attention_heads": 8, "vocab_size": 262144,
                "max_position_embeddings": 131072,
            },
        })
        assert fm["num_hidden_layers"] == 42
        assert fm["hidden_size"] == 2560
        assert fm["dimensions_from"] == "text_config"

    def test_flat_config_does_not_claim_a_source(self):
        fm = _fm({"model_type": "qwen3", "hidden_size": 5120})
        assert fm["hidden_size"] == 5120
        assert "dimensions_from" not in fm

    def test_vision_tower_dimensions_never_win(self):
        """A wrapper carries a `vision_config` with its own `hidden_size`.
        Reporting a tower's dimensions under the field names the flat case uses
        would be indistinguishable from the model's own."""
        fm = _fm({
            "vision_config": {"hidden_size": 1152, "num_hidden_layers": 27},
            "text_config": {"hidden_size": 4096, "num_hidden_layers": 36},
        })
        assert fm["hidden_size"] == 4096
        assert fm["num_hidden_layers"] == 36

    def test_top_level_wins_when_it_carries_more(self):
        fm = _fm({
            "hidden_size": 4096, "num_hidden_layers": 48, "vocab_size": 152576,
            "text_config": {"hidden_size": 1024},
        })
        assert fm["hidden_size"] == 4096
        assert "dimensions_from" not in fm


class TestExpertSpellings:
    """No expert-count spelling is dominant enough to read alone: reading
    `num_experts` by itself missed 7 of 15 sampled MoE configs."""

    def test_n_routed_experts(self):
        """`deepseek-ai/DeepSeek-R1`."""
        fm = _fm({"n_routed_experts": 256, "num_experts_per_tok": 8,
                  "n_shared_experts": 1})
        assert fm["num_experts"] == 256
        assert fm["experts_per_token"] == 8
        assert fm["shared_experts"] == 1

    def test_num_local_experts(self):
        """`openai/gpt-oss-120b`."""
        fm = _fm({"num_local_experts": 128, "num_experts_per_tok": 4})
        assert fm["num_experts"] == 128

    def test_null_shared_expert_count_is_omitted(self):
        fm = _fm({"n_routed_experts": 256, "n_shared_experts": None})
        assert "shared_experts" not in fm

    def test_dense_model_declares_no_experts(self):
        assert "num_experts" not in _fm({"hidden_size": 4096})


class TestQuantBlockSummary:
    def test_per_layer_overrides_are_counted_not_dropped(self):
        """`dawncr0w/MiMo-V2.5-oQ4-MLX` declares `bits=4` beside 181 layer
        overrides at 8-bit.  Dropping the overrides silently turns a
        mixed-precision checkpoint into a uniform-looking one."""
        overrides = {
            f"model.mtp.layers.{i}.eh_proj": {"bits": 8, "group_size": 64}
            for i in range(181)
        }
        fm = _fm({"quantization_config": {"bits": 4, "group_size": 64,
                                          **overrides}})
        assert "bits=4" in fm["quantization"]
        assert "181 per-layer overrides" in fm["quantization"]
        assert "all 8-bit" in fm["quantization"]

    def test_mixed_override_widths_report_a_range(self):
        fm = _fm({"quantization_config": {
            "bits": 4,
            "a": {"bits": 6}, "b": {"bits": 8},
        }})
        assert "6-bit to 8-bit" in fm["quantization"]

    def test_uniform_block_carries_no_override_note(self):
        fm = _fm({"quantization_config": {"bits": 4, "group_size": 128}})
        assert fm["quantization"] == "bits=4, group_size=128"

    def test_present_but_empty_block_still_reported(self):
        fm = _fm({"quantization_config": {}})
        assert fm["quantization"] == "declared, no scalar fields"

    def test_absent_block_stays_silent(self):
        assert "quantization" not in _fm({"model_type": "llama"})


class TestAttentionFrontmatter:
    def test_gqa_ratio(self):
        fm = _fm({"num_attention_heads": 64, "num_key_value_heads": 4})
        assert fm["kv_heads"] == "4 (GQA 16:1)"

    def test_latent_attention_is_not_reported_as_mha(self):
        """`deepseek-ai/DeepSeek-R1` sets `num_key_value_heads` equal to
        `num_attention_heads` and caches a compressed vector instead.  Reading
        that as MHA would be exactly backwards about the model with the
        smallest KV cache."""
        fm = _fm({"num_attention_heads": 128, "num_key_value_heads": 128,
                  "kv_lora_rank": 512})
        assert "MLA" in fm["kv_heads"]
        assert "MHA" not in fm["kv_heads"]

    def test_multi_query_attention(self):
        fm = _fm({"num_attention_heads": 32, "num_key_value_heads": 1})
        assert fm["kv_heads"] == "1 (MQA)"

    def test_rope_type_default_declares_no_scaling(self):
        """`rope_type: default` means the window is native, so naming it would
        imply an extension that is not there."""
        fm = _fm({"rope_scaling": {"rope_type": "default", "type": "default"}})
        assert "rope_scaling" not in fm

    def test_yarn_reports_the_pre_extension_window(self):
        """An extended and a native context report the same
        `max_position_embeddings`; only this says which one it is."""
        fm = _fm({"rope_scaling": {
            "type": "yarn", "factor": 40, "original_max_position_embeddings": 4096,
        }})
        assert fm["rope_scaling"] == "yarn, factor=40, native=4096"

    def test_hybrid_layer_stack_is_summarized(self):
        fm = _fm({"layer_types": ["full_attention", "sliding_attention",
                                  "sliding_attention"]})
        assert fm["attention_pattern"] == "2x sliding_attention, 1x full_attention"

    def test_uniform_layer_stack_says_nothing(self):
        fm = _fm({"layer_types": ["full_attention", "full_attention"]})
        assert "attention_pattern" not in fm


class TestModalities:
    def test_vision_and_audio_towers_are_named(self):
        fm = _fm({"vision_config": {"hidden_size": 1152},
                  "audio_config": {"hidden_size": 1024}})
        assert fm["modalities"] == "text + vision + audio"

    def test_text_only_model_says_nothing(self):
        assert "modalities" not in _fm({"hidden_size": 4096})


# ---------------------------------------------------------------------------
# Fingerprint and format classification
# ---------------------------------------------------------------------------

# Vontra/DeepSeek-V4-Flash-0731-MXFP4-MLX: mxfp4 experts plus mxfp8 attention,
# zero affine modules, zero `.biases` tensors.
_PURE_MX = {
    "BF16": 1_483_567_488,
    "F32": 37_741_630,
    "U8": 18_916_048_896,
    "U32": 38_620_102_656,
    "I64": 2_327_040,
}
# mlx-community/DeepSeek-V4-Flash-8bit: mxfp4/gs32 experts over an affine
# 8-bit/gs64 backbone (512 of its 641 per-module overrides).
_AFFINE_MX_HYBRID = {
    "BF16": 272_765_568,
    "U32": 36_434_673_664,
    "F32": 33_897_431,
    "U8": 8_657_043_456,
    "I64": 2_327_040,
}


class TestNativeFormatClassification:
    """``mode`` is not recoverable from the dtype histogram, so don't claim it.

    Both fixtures above are MLX checkpoints of one model family and produce
    the same buckets (U32 payload, U8 E8M0 scales, BF16 remainder).  One is
    pure mxfp4/mxfp8 with no affine module anywhere; the other runs an affine
    8-bit backbone under mxfp4 experts.  Naming either "affine" is a coin
    flip, and on an FP4-native base "affine" is the string that signals a
    destructive regrid, so guessing it wrong is not a cosmetic error.
    """

    def test_neither_shape_is_called_affine(self):
        for dtypes in (_PURE_MX, _AFFINE_MX_HYBRID):
            out = _classify_native_format(dtypes)
            assert out is not None
            assert "affine" not in out
            assert "E8M0" in out

    def test_fp_grid_ordering_runs_backwards(self):
        """Why no threshold on this histogram recovers the mode.

        The pure-mx repo scores *lower* than the hybrid, because the hybrid's
        affine share is 2.6% of the model and vanishes into BF16 beside the
        experts' scale array.  Pinning the inversion keeps the comment in
        ``_classify_native_format`` from being quietly falsified.
        """
        def fp_grid(d):
            e8m0 = d.get("U8", 0) + d.get("F8_E8M0", 0)
            return e8m0 / (e8m0 + d.get("BF16", 0) + d.get("F16", 0))

        assert fp_grid(_PURE_MX) == pytest.approx(0.927, abs=0.002)
        assert fp_grid(_AFFINE_MX_HYBRID) == pytest.approx(0.970, abs=0.002)
        assert fp_grid(_PURE_MX) < fp_grid(_AFFINE_MX_HYBRID)

    def test_u32_without_e8m0_scales_is_affine(self):
        """Float scales with no E8M0 bucket is affine's own signature."""
        assert _classify_native_format(
            {"U32": 39_628_000_000, "BF16": 10_465_000_000},
        ) == "affine"


class TestDeclaredQuantBlock:
    """The declared block enumerates modules, so it can support an absence."""

    def test_richer_block_wins(self):
        """Vontra mirrors only scalars into `quantization_config` while its
        real 1,177-entry map lives under `quantization`. Reading the
        conventional key alone loses the entire census."""
        cfg = {
            "quantization_config": {"bits": 4, "group_size": 32, "mode": "mxfp4"},
            "quantization": {
                "bits": 4, "group_size": 32, "mode": "mxfp4",
                "layers.0.attn.wq_a": {"bits": 8, "group_size": 32, "mode": "mxfp8"},
            },
        }
        assert "layers.0.attn.wq_a" in _quant_block(cfg)

    def test_affine_default_with_no_overrides_is_decidable(self):
        """A block declaring `mode: affine` and nothing else is all-affine.

        Consulting the override census first returns None here and calls a
        fully-affine repo undecidable, which loses the regrid verdict on the
        simplest possible input.
        """
        assert _has_affine_module({"bits": 4, "group_size": 64, "mode": "affine"}) is True

    def test_absence_needs_an_enumerated_domain(self):
        """No overrides and a non-affine default is genuinely undecidable."""
        assert _has_affine_module({"bits": 4, "mode": "mxfp4"}) is None

    def test_pure_mx_map_proves_no_affine(self):
        """Vontra's shape: mxfp4 default, mxfp8 overrides, nothing affine."""
        assert _has_affine_module(_CFG_QUANT_PURE_MX["quantization"]) is False

    def test_nvfp4_is_an_fp_grid_not_an_absence(self):
        """E4M3 scales at group 16 are a microscaling FP grid. A vocabulary
        scoped to E8M0 reports this as no FP grid and invites the regrid."""
        block = _CFG_QUANT_NVFP4["quantization_config"]
        assert _declared_grid(block) == ["nvfp4-e4m3"]

    def test_hybrid_declares_both_grids(self):
        """nvidia/DeepSeek-V4-Flash-NVFP4 retained part of its base's E8M0
        backbone while converting the experts to NVFP4."""
        block = {
            "quant_method": "fp8", "fmt": "e4m3", "scale_fmt": "ue8m0",
            "moe_quant_algo": "NVFP4", "quant_algo": "MIXED_PRECISION",
        }
        assert _declared_grid(block) == ["mx-e8m0", "nvfp4-e4m3"]


class TestHistogramScope:
    """A zero ``fp_grid`` is only a measurement if the histogram can carry one.

    ``deepseek-ai/DeepSeek-V4-Flash-0731`` publishes no ``F8_E8M0`` bucket at
    all, yet a range-read of any middle shard finds 776 E8M0 tensors in it
    (``layers.0.attn.wkv.scale`` and siblings).  The Hub's aggregate omits
    them, while reporting them for ``deepseek-ai/DeepSeek-V4-Flash``, the
    preview release of the same model in the same format.

    Reading 0.0 off that gap and calling it "no native FP grid to preserve"
    is the reassuring direction of wrong: it green-lights the affine regrid
    the detector exists to warn about.
    """

    def test_parameter_scoped_repo_is_detected(self):
        """0731's real histogram: I8 counts logical weights, scales omitted."""
        assert _histogram_is_storage_scoped(
            _BASE_FP4_NO_SCALES, int(155.43 * GIB),
        ) is False

    def test_storage_scoped_repo_passes(self):
        """The preview repo reconciles at 1.000, so 0.86 is a real value."""
        assert _histogram_is_storage_scoped(_BASE_FP4, int(148.66 * GIB)) is True

    def test_per_channel_scales_are_not_penalised(self):
        """ixim/Qwen-Image-2512-Quanto-INT8-Full: per-channel INT8.

        One scale per output channel puts the effective group at ``in_features``
        (~1,835 here), far above any group-wise width. Its histogram is complete
        and storage-scoped, so its fp_grid of 0.0 is a real measurement and must
        survive.
        """
        quanto = {"I8": 20_424_818_688, "BF16": 11_130_496}
        assert _histogram_is_storage_scoped(quanto, 20_436_000_000) is True

    def test_packed_u8_payload_is_parameter_scoped(self):
        """openai/gpt-oss-120b's U8 bucket is packed mxfp4 payload.

        Its element counts are logical weights, so implied bytes overshoot the
        checkpoint by the packing factor and no scale-derived signal off this
        histogram is founded.
        """
        gpt_oss = {"U8": 118_245_000_000, "BF16": 2_167_000_000}
        assert _histogram_is_storage_scoped(gpt_oss, int(60.77 * GIB)) is False

    def test_fp_grid_abstains_when_scales_are_missing(self):
        report = analyze_quant(_payload(
            safetensors={
                "parameters": _BASE_FP4_NO_SCALES, "total": 304_180_418_494,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 48, int(155.43 * GIB)),
            ],
            config={"quantization_config": {"quant_method": "fp8"}},
        ))
        assert report.fp_grid is None
        # The number that *is* trustworthy here must still come through.
        assert report.bpw == pytest.approx(4.39, abs=0.02)

    def test_bare_u8_payload_is_not_a_scale_array(self):
        """NVFP4 stores packed payload in U8 and its scales in F8_E4M3.

        nvidia/Qwen3.6-35B-A3B-NVFP4 and the RedHatAI mirror both scored
        fp_grid ~0.90 and printed "FP-microscaling grid present (E8M0 scales)"
        over headers containing zero E8M0 tensors: `weight_packed:U8` beside
        `weight_scale:F8_E4M3`. Asserting a dtype the file does not contain is
        worse than the silence the surrounding gates were written to fix.

        MLX pairs a U32 payload with a U8 scale array, so U8 means scales only
        in that company. Bare U8 abstains.
        """
        nvfp4 = {
            "BF16": 1_825_916_784,
            "F8_E4M3": 3_332_177_920,
            "U8": 16_423_321_600,
        }
        report = analyze_quant(_payload(
            safetensors={"parameters": nvfp4, "total": 18_683_860_336},
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 3, int(21.81 * GIB)),
            ],
            config={"quantization_config": {"quant_method": "modelopt"}},
        ))
        assert report.fp_grid is None

    def test_u8_beside_u32_still_counts_as_mlx_scales(self):
        """The guard must not break the MLX convention it exists to serve."""
        report = analyze_quant(_payload(
            safetensors={"parameters": _PURE_MX, "total": 59_059_787_710},
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 34, int(155.61 * GIB)),
            ],
            config={"quantization_config": {"bits": 4}},
        ))
        assert report.fp_grid == pytest.approx(0.927, abs=0.002)

    def test_parameter_scoped_histogram_abstains_but_keeps_bpw(self):
        """openai/gpt-oss-120b end to end: no grid signal, bpw untouched.

        Its fp_grid was 0.982, computed as payload over payload plus BF16.
        The conclusion "microscaling grid present" is true for this repo, but
        nothing in that arithmetic established it, so it must abstain. bpw
        rests on `total`, not on the scale buckets, and is unaffected.
        """
        report = analyze_quant(_payload(
            safetensors={
                "parameters": {"U8": 118_245_000_000, "BF16": 2_167_000_000},
                "total": 120_412_000_000,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 15, int(60.77 * GIB), start=0),
            ],
            config={"quantization_config": {"quant_method": "mxfp4"}},
        ))
        assert report.fp_grid is None
        assert report.bpw == pytest.approx(4.34, abs=0.02)


class TestDtypeFingerprint:
    def test_drops_trivial_housekeeping_buckets(self):
        out = _format_dtype_fingerprint({
            "U32": 77_182_000_000, "BF16": 19_345_000_000, "F32": 12_032,
        })
        assert out is not None
        assert "U32" in out
        assert "BF16" in out
        assert "F32" not in out

    def test_empty_returns_none(self):
        assert _format_dtype_fingerprint({}) is None

    def test_all_tiny_buckets_still_render(self):
        """A repo whose every bucket is small must not render as nothing."""
        assert _format_dtype_fingerprint({"F32": 10, "I64": 5}) is not None

    def test_e8m0_scales_counted_in_both_spellings(self):
        """MLX packs E8M0 scales into U8; a native release reports F8_E8M0.

        Counting only U8 scores a preserved FP grid at 0.0 and calls it
        all-affine, the opposite of the truth.
        """
        native = analyze_quant(_payload(
            safetensors={
                "parameters": {"I8": 141_734_000_000, "F8_E8M0": 8_859_000_000,
                               "F8_E4M3": 6_023_000_000, "BF16": 1_415_000_000},
                "total": 158_066_000_000,
            },
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 46, int(148.66 * GIB)),
            ],
        ))
        assert native.fp_grid is not None and native.fp_grid > 0.5
        assert native.native_format is not None
        assert "FP4-packed" in native.native_format
        assert "FP8" in native.native_format


class TestFamilyStem:
    @pytest.mark.parametrize("repo,expected", [
        ("inferencerlabs/MiMo-V2.5-LM-MLX-Q9", "MiMo-V2.5"),
        ("dawncr0w/MiMo-V2.5-oQ4-MLX", "MiMo-V2.5"),
        ("mlx-community/DeepSeek-V4-Flash-8bit", "DeepSeek-V4-Flash"),
        ("unsloth/DeepSeek-V3.1-GGUF", "DeepSeek-V3.1"),
        ("openai/gpt-oss-120b", "gpt-oss-120b"),
    ])
    def test_strips_stacked_quant_suffixes(self, repo, expected):
        assert _family_stem(repo) == expected


# ---------------------------------------------------------------------------
# URL detection
# ---------------------------------------------------------------------------

class TestUrlDetection:
    @pytest.mark.parametrize("url,kind", [
        ("https://huggingface.co/openai/gpt-oss-120b", "model"),
        ("https://huggingface.co/openai/gpt-oss-120b/tree/main", "tree"),
        ("https://huggingface.co/openai/gpt-oss-120b/blob/main/config.json", "file"),
        ("https://huggingface.co/openai/gpt-oss-120b/resolve/main/a.safetensors", "file"),
        ("https://huggingface.co/openai/gpt-oss-120b/raw/main/README.md", "file"),
        ("https://huggingface.co/mlx-community", "org"),
        ("https://huggingface.co/datasets/rajpurkar/squad", "dataset"),
        ("https://huggingface.co/spaces/gradio/hello", "space"),
    ])
    def test_kinds(self, url, kind):
        match = _detect_hf_url(url)
        assert match is not None
        assert match.kind == kind

    @pytest.mark.parametrize("url", [
        "https://huggingface.co/models?search=mimo",
        "https://huggingface.co/login",
        "https://huggingface.co/",
        "https://example.com/openai/gpt-oss-120b",
    ])
    def test_non_repo_urls_return_none(self, url):
        """Hub feature paths must not parse as an org named "models"."""
        assert _detect_hf_url(url) is None

    def test_commit_url_carries_sha(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        match = _detect_hf_url(f"https://huggingface.co/o/m/commit/{sha}")
        assert match is not None
        assert match.kind == "commit"
        assert match.sha == sha

    def test_commit_sha_is_immutable(self):
        assert is_hf_commit_sha("0123456789abcdef0123456789abcdef01234567")
        assert not is_hf_commit_sha("main")

    def test_unknown_repo_tab_falls_back_to_model(self):
        match = _detect_hf_url("https://huggingface.co/o/m/discussions")
        assert match is not None
        assert match.kind == "model"
        assert match.repo == "o/m"


class TestQueryParsing:
    def test_repo_at_revision(self):
        assert _split_repo_rev("org/model@abc123") == ("org/model", "abc123")

    def test_bare_repo_defaults_to_main(self):
        assert _split_repo_rev("org/model") == ("org/model", "main")

    def test_garbage_rejected(self):
        assert _split_repo_rev("not a repo")[0] == ""

    def test_file_path_split(self):
        assert _split_repo_path("org/model/sub/config.json") == (
            "org/model", "sub/config.json", "main",
        )


# ---------------------------------------------------------------------------
# Rate limit parsing
# ---------------------------------------------------------------------------

class TestRateLimitParsing:
    def test_parses_rfc_9651_structured_fields(self):
        """The Hub does not send X-RateLimit-*; params trail a quoted bucket
        name, so `int(header)` is not the parse."""
        rl = _HFRateLimit.from_headers(httpx.Headers({
            "ratelimit": '"api";r=495;t=140',
            "ratelimit-policy": '"fixed window";"api";q=500;w=300',
        }))
        assert rl is not None
        assert rl.bucket == "api"
        assert rl.remaining == 495
        assert rl.reset_seconds == 140
        assert rl.quota == 500
        assert rl.window == 300

    def test_absent_header_returns_none(self):
        assert _HFRateLimit.from_headers(httpx.Headers({})) is None

    def test_malformed_header_does_not_raise(self):
        rl = _HFRateLimit.from_headers(httpx.Headers({"ratelimit": "garbage"}))
        assert rl is not None
        assert rl.remaining is None


# ---------------------------------------------------------------------------
# HTTP behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRequestErrors:
    @respx.mock
    async def test_401_is_honestly_ambiguous(self):
        """Gated-invisible, private, and nonexistent are byte-identical 401s
        when unauthenticated. Naming one of them would be a confident lie."""
        respx.get("https://huggingface.co/api/models/x/y").mock(
            return_value=httpx.Response(
                401, json={"error": "Invalid username or password."},
            ),
        )
        result = await _hf_request("/models/x/y", repo="x/y")
        assert result.startswith("Error:")
        assert "gated, private, or does not exist" in result

    @respx.mock
    async def test_400_surfaces_the_hub_error_body(self):
        """The Hub enumerates valid `expand` tokens in its 400 body, which is
        a live drift signal worth showing rather than swallowing."""
        respx.get("https://huggingface.co/api/models/x/y").mock(
            return_value=httpx.Response(
                400, json={"error": '"expand" must be one of [author, config]'},
            ),
        )
        result = await _hf_request("/models/x/y", repo="x/y")
        assert '"expand" must be one of' in result

    @respx.mock
    async def test_5xx_retries_then_reports(self):
        route = respx.get("https://huggingface.co/api/models/x/y").mock(
            return_value=httpx.Response(503),
        )
        result = await _hf_request("/models/x/y", repo="x/y")
        assert "503" in result
        assert route.call_count > 1


# ---------------------------------------------------------------------------
# Tool surface
# ---------------------------------------------------------------------------

def test_fm_base_seeds_trust():
    """Every action fences uploader-controlled content, so the advisory must
    originate in one place."""
    assert _fm_base("https://huggingface.co/o/m")["trust"] == _TRUST_ADVISORY


@pytest.mark.asyncio
class TestPageCachePopulation:
    """The fast path must cache full content while returning a truncated view.

    Without this the tool truncates, tells the caller to narrow with
    ``section=`` / ``search=``, and then has nothing to narrow: the follow-up
    re-enters the fast path, finds no entry, and returns the same truncated
    text. The steering hint would loop back to itself.
    """

    @respx.mock
    async def test_model_card_is_cached_in_full(self):
        long_card = "# Title\n\n" + "\n\n".join(
            f"## Section {i}\n\n" + ("body text " * 200) for i in range(12)
        )
        respx.get("https://huggingface.co/api/models/org/model").mock(
            return_value=httpx.Response(200, json=_payload()),
        )
        respx.get(
            "https://huggingface.co/org/model/raw/main/README.md",
        ).mock(return_value=httpx.Response(200, text=long_card))

        url = "https://huggingface.co/org/model"
        out = await _hf_fast_path(url)
        assert out is not None

        entry = _page_cache.get(url)
        assert entry is not None
        # The returned view is truncated; the cached copy is not.
        assert len(entry.markdown) > len(out)
        assert "Section 11" in entry.markdown
        assert "Section 11" not in out

    @respx.mock
    async def test_file_body_is_cached_and_sliceable(self):
        weight_map = {
            f"model.layers.{i}.mlp.experts.weight": "model-00001-of-00002.safetensors"
            for i in range(400)
        }
        respx.get(
            "https://huggingface.co/org/model/raw/main/model.safetensors.index.json",
        ).mock(return_value=httpx.Response(
            200, json={"metadata": {"total_size": 1000}, "weight_map": weight_map},
        ))

        url = "https://huggingface.co/org/model/blob/main/model.safetensors.index.json"
        assert await _hf_fast_path(url) is not None
        entry = _page_cache.get(url)
        assert entry is not None
        assert len(entry.slices) > 1

    @respx.mock
    async def test_card_and_file_share_an_eviction_group(self):
        """A repo's card and its config should not outlive each other."""
        respx.get("https://huggingface.co/api/models/org/model").mock(
            return_value=httpx.Response(200, json=_payload()),
        )
        respx.get(
            "https://huggingface.co/org/model/raw/main/README.md",
        ).mock(return_value=httpx.Response(200, text="# Card\n\nbody " * 500))
        respx.get(
            "https://huggingface.co/org/model/raw/main/config.json",
        ).mock(return_value=httpx.Response(200, json={"model_type": "llama"}))

        card_url = "https://huggingface.co/org/model"
        file_url = "https://huggingface.co/org/model/blob/main/config.json"
        await _hf_fast_path(card_url)
        await _hf_fast_path(file_url)

        card_entry = _page_cache.get(card_url)
        file_entry = _page_cache.get(file_url)
        assert card_entry is not None and file_entry is not None
        assert card_entry.group == "hf:org/model@main"
        assert file_entry.group == card_entry.group

    @respx.mock
    async def test_oversize_file_is_rejected_before_caching(self):
        """The size cap is the outer guard; nothing that large reaches the
        splitter, and nothing is cached on the rejection path."""
        respx.get(
            "https://huggingface.co/org/model/raw/main/blob.txt",
        ).mock(return_value=httpx.Response(200, text="x" * 2_000_000))

        url = "https://huggingface.co/org/model/blob/main/blob.txt"
        out = await _hf_fast_path(url)
        assert out is not None
        assert out.startswith("Error")
        assert _page_cache.get(url) is None

    @respx.mock
    async def test_oversize_json_is_also_rejected(self):
        """Regression: the cap must apply to the rendered form, not just the
        text branch. Gating only `text` exempts every JSON file, and a
        weight_map is exactly the JSON that runs to tens of MB."""
        huge = {f"key_{i}": "v" * 100 for i in range(20_000)}
        respx.get(
            "https://huggingface.co/org/model/raw/main/big.json",
        ).mock(return_value=httpx.Response(200, json=huge))

        url = "https://huggingface.co/org/model/blob/main/big.json"
        out = await _hf_fast_path(url)
        assert out is not None
        assert out.startswith("Error")
        assert _page_cache.get(url) is None

    @respx.mock
    async def test_tool_path_without_cache_url_does_not_cache(self):
        """Only the fetch-interception path has a URL worth keying on."""
        respx.get("https://huggingface.co/api/models/org/model").mock(
            return_value=httpx.Response(200, json=_payload()),
        )
        respx.get(
            "https://huggingface.co/org/model/raw/main/README.md",
        ).mock(return_value=httpx.Response(200, text="# Card\n\nbody"))
        out = await huggingface("model", "org/model")
        assert not out.startswith("Error")
        assert _page_cache.get("https://huggingface.co/org/model") is None


# mlx-community/DeepSeek-V4-Flash-8bit shape: mxfp4 experts, E8M0 scales.
_QUANT_MX = {"U32": 36_434_673_664, "U8": 8_657_043_456, "BF16": 272_765_568}
# The damage case: every scale is float, so the FP lattice is gone.
_QUANT_AFFINE = {"U32": 39_628_000_000, "BF16": 10_465_000_000}
# deepseek-ai/DeepSeek-V4-Flash shape: FP4 experts over an FP8 backbone.
# The F32 and I64 housekeeping buckets are not filler: with them the counts sum
# to exactly `total`, which is precondition 1's trigger and the reason this
# base's own bpw is suppressed. Drop them and sigma falls 38.5M short, the
# guard stays silent, and the fixture models a repo that does not exist.
_BASE_FP4 = {
    "I8": 141_733_920_768,
    "F8_E8M0": 8_858_737_664,
    "F8_E4M3": 6_023_020_544,
    "BF16": 1_415_259_264,
    "F32": 36_168_018,
    "I64": 2_327_040,
}
# deepseek-ai/DeepSeek-V4-Flash-0731: the same vendor and the same FP4+FP8
# format, but the Hub published no F8_E8M0 bucket for it.  The scales are in
# the file (776 E8M0 tensors per shard by range-read); only the aggregate
# omits them.
_BASE_FP4_NO_SCALES = {
    "I8": 296_352_743_424,
    "F8_E4M3": 6_304_038_912,
    "BF16": 1_483_567_488,
    "F32": 37_741_630,
    "I64": 2_327_040,
}


# Real shapes, transcribed live. The base is deepseek-ai/DeepSeek-V4-Flash's
# 1.9 KB block, whose `scale_fmt` is the whole answer to the grid question the
# dtype histogram could not reach.
_CFG_BASE_UE8M0 = {"quantization_config": {
    "quant_method": "fp8", "fmt": "e4m3", "scale_fmt": "ue8m0",
    "activation_scheme": "dynamic",
}}
# Vontra's shape: MLX writes the map under `quantization`, mxfp4 default with
# mxfp8 overrides and no affine module anywhere.
_CFG_QUANT_PURE_MX = {"quantization": {
    "bits": 4, "group_size": 32, "mode": "mxfp4",
    "layers.0.attn.wq_a": {"bits": 8, "group_size": 32, "mode": "mxfp8"},
    "layers.0.ffn.gate": False,
}}
# The damage case: an affine default collapses the FP lattice onto integers.
_CFG_QUANT_AFFINE = {"quantization": {
    "bits": 4, "group_size": 64, "mode": "affine",
    "layers.0.attn.wq_a": {"bits": 8, "group_size": 64, "mode": "affine"},
}}
# nvidia/Qwen3.6-35B-A3B-NVFP4: a microscaling FP4 grid with E4M3 scales at
# group 16, which a vocabulary scoped to E8M0 would call "no FP grid".
_CFG_QUANT_NVFP4 = {"quantization_config": {
    "quant_method": "modelopt", "quant_algo": "MIXED_PRECISION",
    "config_groups": {
        "group_0": {"weights": {"num_bits": 4, "type": "float", "group_size": 16}},
    },
}}


def _mock_audit_pair(
    quant_dtypes: dict,
    base_dtypes: dict = _BASE_FP4,
    *,
    quant_config: dict | None = None,
    base_config: dict | None = None,
    quant_total: int = 284_333_146_519,
) -> None:
    """Mock a quant repo declaring lineage plus the FP4-native base it names."""
    respx.get("https://huggingface.co/org/quant/raw/main/config.json").mock(
        return_value=httpx.Response(200, json=quant_config or {}),
    )
    respx.get("https://huggingface.co/org/base/raw/main/config.json").mock(
        return_value=httpx.Response(200, json=base_config or {}),
    )
    respx.get("https://huggingface.co/api/models/org/quant").mock(
        return_value=httpx.Response(200, json=_payload(
            id="org/quant",
            safetensors={"parameters": quant_dtypes, "total": quant_total},
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 33, int(144.44 * GIB)),
            ],
            baseModels={
                "relation": "quantized", "models": [{"id": "org/base"}],
            },
        )),
    )
    respx.get("https://huggingface.co/org/quant/raw/main/README.md").mock(
        return_value=httpx.Response(200, text="# Quant"),
    )
    respx.get("https://huggingface.co/api/models/org/base").mock(
        return_value=httpx.Response(200, json=_payload(
            id="org/base",
            safetensors={"parameters": base_dtypes, "total": 158_069_433_298},
            siblings=[
                {"rfilename": "model.safetensors.index.json", "size": 100},
                *_shards("model", 46, int(148.66 * GIB)),
            ],
        )),
    )


@pytest.mark.asyncio
class TestQuantAuditGridVerdict:
    """Tier 1 owns the preservation verdict because only it holds both grids.

    The dtype fingerprint shows whether *this* repo carries E8M0 scales.
    Whether that matches the base's grid is a different claim needing the base
    fetched, which is exactly what ``quant_audit=true`` buys.  Asserting it
    from one side fired on vendor base repos about their own native format.
    """

    @respx.mock
    async def test_mx_over_fp4_base_reports_preserved(self):
        """Both declare mx-e8m0, and the quant's map names no affine module."""
        _mock_audit_pair(
            _QUANT_MX,
            quant_config=_CFG_QUANT_PURE_MX, base_config=_CFG_BASE_UE8M0,
        )
        out = await huggingface("model", "org/quant", quant_audit=True)
        assert "grid preserved" in out
        assert "no module is affine" in out

    @respx.mock
    async def test_affine_over_fp4_base_reports_damage(self):
        """The case the detector exists for: a native FP4 lattice requantized
        onto a uniform one.  No size or bpw signal shows this, and the
        downloader has no way to notice it after the fact.

        Reachable only because the declared map enumerates the repo's modules.
        The dtype histogram cannot support the underlying absence claim, and
        one shard header samples a fraction systematically biased toward the
        expert tensors that carry no bias at all.
        """
        _mock_audit_pair(
            _QUANT_AFFINE,
            quant_config=_CFG_QUANT_AFFINE, base_config=_CFG_BASE_UE8M0,
        )
        out = await huggingface("model", "org/quant", quant_audit=True)
        # Assert the Tier 2 wording specifically. The histogram fallback emits
        # the same "grid NOT preserved" headline from far weaker evidence, so
        # the headline alone cannot tell which source ruled.
        assert "grid NOT preserved" in out
        assert "this repo declares affine modules" in out

    @respx.mock
    async def test_float_to_float_regrid_is_not_called_damage(self):
        """NVFP4 over an E8M0 base is a regrid, not a collapse onto integers.

        A vocabulary scoped to E8M0 alone reports NVFP4 as having no FP grid,
        which invites exactly the affine requant that would destroy it. Both
        sides are non-uniform float lattices and the verdict must say so.
        """
        _mock_audit_pair(
            _QUANT_AFFINE,
            quant_config=_CFG_QUANT_NVFP4, base_config=_CFG_BASE_UE8M0,
        )
        out = await huggingface("model", "org/quant", quant_audit=True)
        assert "grid CHANGED" in out
        assert "nvfp4-e4m3" in out
        assert "grid NOT preserved" not in out

    @respx.mock
    async def test_declared_config_beats_an_unreadable_histogram(self):
        """deepseek-ai/DeepSeek-V4-Flash-0731's shape: the Hub publishes no
        E8M0 bucket for it, so Tier 0 abstains, but its 1.9 KB config declares
        `scale_fmt: ue8m0` outright. The declared block is what the verdict
        rests on, so the audit answers instead of shrugging.
        """
        _mock_audit_pair(
            _QUANT_MX, base_dtypes=_BASE_FP4_NO_SCALES,
            quant_config=_CFG_QUANT_PURE_MX, base_config=_CFG_BASE_UE8M0,
        )
        out = await huggingface("model", "org/quant", quant_audit=True)
        assert "base declares grid" in out
        assert "mx-e8m0" in out
        assert "grid preserved" in out

    @respx.mock
    async def test_no_lineage_still_reports_the_repos_own_map(self):
        """Jundot/DeepSeek-V4-Flash-0731-oQ4e-mtp declares no base_model.

        Without this the audit returns one line saying there is nothing to
        compare against and discards the repo's own declared block. That block
        names 147 affine modules, which is most of what a caller comparing
        sibling quants needs; only the other side is missing.
        """
        respx.get("https://huggingface.co/api/models/org/solo").mock(
            return_value=httpx.Response(200, json=_payload(
                id="org/solo",
                safetensors={"parameters": _QUANT_AFFINE, "total": 50_093_000_000},
                siblings=[
                    {"rfilename": "model.safetensors.index.json", "size": 100},
                    *_shards("model", 30, int(167.12 * GIB)),
                ],
            )),
        )
        respx.get("https://huggingface.co/org/solo/raw/main/README.md").mock(
            return_value=httpx.Response(200, text="# Solo"),
        )
        respx.get("https://huggingface.co/org/solo/raw/main/config.json").mock(
            return_value=httpx.Response(200, json=_CFG_QUANT_AFFINE),
        )
        out = await huggingface("model", "org/solo", quant_audit=True)
        assert "this repo declares grid: affine" in out
        assert "per-module map" in out
        assert "no base model is declared" in out

    @respx.mock
    async def test_no_substitution_from_a_rejected_base_count(self):
        """A base count rejected as its own denominator cannot serve as one.

        deepseek-ai/DeepSeek-V4-Flash reports 158.1B storage elements against
        ~291B logical weights, so precondition 1 suppresses its own bpw.
        Reusing it for a child puts NVFP4 conversions at 8.52 bpw against a
        true ~4.4, inflating the child by the same factor the precondition
        catches on the parent.
        """
        # The quant's own bpw must be suppressed for the substitution branch to
        # be reachable at all: U32 present with total == sigma is precondition
        # 1's exact signature.
        _mock_audit_pair(
            _QUANT_AFFINE, base_dtypes=_BASE_FP4,
            quant_config=_CFG_QUANT_AFFINE, base_config=_CFG_BASE_UE8M0,
            quant_total=50_093_000_000,
        )
        out = await huggingface("model", "org/quant", quant_audit=True)
        assert "effective bits-per-weight not reported" in out
        assert "when measured against the base's parameter count" not in out

    @respx.mock
    async def test_no_declared_blocks_falls_back_and_says_so(self):
        """With both configs empty the histogram fallback runs, and its
        message must not assert a specific cause it cannot know."""
        _mock_audit_pair(_QUANT_AFFINE, base_dtypes=_BASE_FP4_NO_SCALES)
        out = await huggingface("model", "org/quant", quant_audit=True)
        assert "grid verdict unavailable" in out
        assert "no native FP grid to preserve" not in out


def test_presplit_failure_skips_cache():
    """A single line over the presplit ceiling is the issue-#6 splitter DoS
    vector; caching is skipped rather than routed into it.

    Exercised directly because the file action's size cap (identical
    threshold) rejects such content before it could ever reach the splitter.
    The skip still has to be correct for any future caller that does not sit
    behind that cap.
    """
    url = "https://huggingface.co/org/model/blob/main/x.txt"
    _cache_file_body(
        url, "org/model", "x.txt",
        rev="main", basename="x.txt", rendered="y" * 1_500_000,
    )
    assert _page_cache.get(url) is None


@pytest.mark.asyncio
class TestToolDispatch:
    async def test_unknown_action_rejected(self):
        out = await huggingface("frobnicate", "org/model")
        assert out.startswith("Error: Unknown action")

    async def test_dataset_url_declined_explicitly(self):
        """A dataset answered by the model handler would be confidently wrong,
        so v1 declines rather than guessing."""
        out = await huggingface(
            "model", "https://huggingface.co/datasets/rajpurkar/squad",
        )
        assert out.startswith("Error:")
        assert "dataset" in out

    @respx.mock
    async def test_weight_file_is_never_downloaded(self):
        """A .safetensors read returns the range-read recipe, not the payload."""
        respx.get(
            "https://huggingface.co/api/models/org/model",
        ).mock(return_value=httpx.Response(200, json=_payload(
            siblings=[{
                "rfilename": "model-00001-of-00002.safetensors",
                "size": 5 * GIB,
                "lfs": {"sha256": "abc123"},
            }],
        )))
        out = await huggingface(
            "file", "org/model/model-00001-of-00002.safetensors",
        )
        assert "not transferred" in out
        assert "Range: bytes=0-7" in out
        assert "abc123" in out

    @respx.mock
    async def test_gated_repo_warns_with_the_enum(self):
        """`gated` is tri-state; the distinction between instant click-through
        and a human approval queue is the actionable part."""
        respx.get("https://huggingface.co/api/models/meta/llama").mock(
            return_value=httpx.Response(200, json=_payload(gated="manual")),
        )
        respx.get(
            "https://huggingface.co/meta/llama/raw/main/README.md",
        ).mock(return_value=httpx.Response(404))
        out = await huggingface("model", "meta/llama")
        assert "gated: manual" in out
        assert "manual approval" in out

    @respx.mock
    async def test_bpw_suppression_reaches_frontmatter(self):
        """A suppressed number is a correct output, so the envelope must say
        why rather than silently omitting it."""
        respx.get("https://huggingface.co/api/models/d/oq4").mock(
            return_value=httpx.Response(200, json=_payload(
                safetensors={
                    "parameters": {"U32": 39_628_000_000, "BF16": 10_465_000_000},
                    "total": 50_093_000_000,
                },
                siblings=[
                    {"rfilename": "model.safetensors.index.json", "size": 100},
                    *_shards("model", 30, int(167.12 * GIB)),
                ],
                config={"quantization_config": {"bits": 4}},
            )),
        )
        respx.get(
            "https://huggingface.co/d/oq4/raw/main/README.md",
        ).mock(return_value=httpx.Response(404))
        out = await huggingface("model", "d/oq4")
        assert "effective bits-per-weight not reported" in out
        assert "28." not in out.split("┌─ untrusted")[0]
