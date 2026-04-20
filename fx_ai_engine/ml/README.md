# fx_ai_engine/ml/ — Machine Learning Subsystem

**Phase 1 COMPLETE:** data extraction, signal replay, labeling, features, validation, baseline.
**183 unit tests, all passing.** Full end-to-end demo verified.

## What's in this release

### `ml/features/` — Feature engineering (production + training)

| File | Purpose |
|---|---|
| `schema.py` | Frozen contract for feature order and schema version |
| `indicators.py` | Pure pandas/numpy EMA, SMA, ATR, RSI, ADX, MACD, Bollinger, Stochastic (Wilder smoothing) |
| `session.py` | UTC hour, day-of-week, session flags, regime encoding |
| `microstructure.py` | Spread→pips, volume z-score/ratio vs prior window |
| `builder.py` | `FeatureBuilder` — emits 30-feature `FeatureVector` for any timestamp |

### `ml/meta_labeler/`

| File | Purpose |
|---|---|
| `extract_data.py` | MT5 bar extraction to Parquet + manifest (CLI-runnable) |
| `signal_replay.py` | Walk history calling your agents → generates training candidates |
| `label.py` | Triple-barrier labeling (WIN/LOSS/TIMEOUT) + batch labeling |
| `validation.py` | Walk-forward folds + PurgedKFold + strategy Sharpe metric |
| `train_baseline.py` | Logistic regression baseline with walk-forward CV |

### `ml/examples/`

| File | Purpose |
|---|---|
| `end_to_end_demo.py` | Runs the FULL Phase 1 pipeline on synthetic data |

### `ml/tests/` — 183 unit tests, all passing

| File | Tests |
|---|---|
| `conftest.py` | Synthetic M15/H1 fixtures |
| `test_indicators.py` | 28 tests — mathematical correctness of every indicator |
| `test_session.py` | 17 tests — time/session/regime encoding |
| `test_microstructure.py` | 14 tests — spread/volume features |
| `test_builder.py` | 22 tests — FeatureBuilder, schema contract, no-look-ahead |
| `test_extract_data.py` | 22 tests — MT5 extraction (mocked), Parquet round-trip |
| `test_signal_replay.py` | 25 tests — protocols, stubs, replay loop |
| `test_label.py` | 26 tests — BUY/SELL outcomes, timeouts, edge cases |
| `test_validation.py` | 17 tests — walk-forward, PurgedKFold, metrics |
| `test_train_baseline.py` | 12 tests — learnable vs noise datasets |

## Running the tests

```bash
cd fx_ai_engine
python3 -m pytest ml/tests/ -v
```

Expected: `183 passed in ~6s`.

## Running the full end-to-end demo (no MT5 required)

```bash
cd fx_ai_engine
python3 -m ml.examples.end_to_end_demo
```

This runs synthetic data through every Phase 1 module — extraction → replay → labeling → features → validation → baseline training. Proves the pipeline is wired correctly.

## Running on real MT5 data (Windows + live terminal)

```bash
# 1. Extract bars for 6 majors × M15+H1 × 5 years
python3 -m ml.meta_labeler.extract_data

# 2. From Python, plug in your real RegimeAgent + TechnicalAgent
from ml.meta_labeler import extract_data
from ml.meta_labeler.signal_replay import replay_signals
from ml.meta_labeler.label import label_all, label_summary
from core.agents.regime_agent import RegimeAgent       # your existing
from core.agents.technical_agent import TechnicalAgent # your existing

m15 = extract_data.load_parquet("EURUSD", "M15")
h1  = extract_data.load_parquet("EURUSD", "H1")

candidates = replay_signals(m15, h1, "EURUSD",
    regime_agent=RegimeAgentAdapter(RegimeAgent()),
    technical_agent=TechnicalAgentAdapter(TechnicalAgent()))
labeled = label_all(candidates, m15, ttl_bars=24)
print(label_summary(labeled))
```

## Integration with your existing AdversarialAgent

Your existing `RegimeAgent` and `TechnicalAgent` need thin adapters to satisfy the Protocols in `signal_replay.py`. The module includes `ConstantRegimeAgent` and `SimpleEMACrossTechnicalAgent` as reference implementations showing the expected shape.

## Phase 1 Definition of Done — Status

- [x] All 6 symbols × 2 timeframes extractable via `extract_data.py`
- [x] Signal replay preserves point-in-time (verified by `test_point_in_time_no_lookahead`)
- [x] Triple-barrier labels match manually-computed outcomes on known sequences
- [x] 30-feature vector computable for any candidate; feature parity guaranteed
- [x] Walk-forward harness produces folds with no train/test overlap, purge + embargo respected
- [x] Baseline training returns metrics dict with acceptance thresholds
- [x] End-to-end demo runs without errors
- [x] Unit tests cover all edge cases
- [ ] Baseline achieves ≥ 0.3 median Sharpe on REAL data (requires MT5 + your real agents)
- [ ] MLflow tracking server running (optional — module falls back gracefully if not installed)

## What's next (Phase 2)

The Phase 2 modules to build after validating Phase 1 on real data:

- `train_lgbm.py` — LightGBM + Optuna (100-trial hyperparameter search)
- `evaluate.py` — SHAP analysis + per-regime/pair breakdown + model card
- `export_onnx.py` — ONNX serialization with embedded schema version
- `client.py` — `MetaLabelerClient` for live inference
- `drift.py` — PSI drift detector
- `shadow_runner.py` — Shadow-mode integration into `AdversarialAgent`
- `promote.py` — 7-gate promotion/rollback logic

## Dependencies

| Package | Version | Why |
|---|---|---|
| pandas | ≥2.0 | DataFrame operations |
| numpy | **<2.0** | MT5 Python package ABI compatibility — REQUIRED |
| scikit-learn | ≥1.3 | Baseline LogisticRegression |
| pyarrow | ≥10 | Parquet I/O |
| pytest | ≥7 | Tests |
| MetaTrader5 | latest | Data extraction (Windows only) |
| mlflow | optional | Experiment tracking (falls back gracefully) |

Install minimum:
```bash
pip install "pandas>=2.0" "numpy<2.0" scikit-learn pyarrow pytest
# For extraction on Windows:
pip install MetaTrader5
# Optional:
pip install mlflow
```
