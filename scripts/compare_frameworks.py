"""Exercise 3.4 — compare RAGAS and DeepEval on the same saved answers.

Reads golden_dataset.json + artifacts/actual_answers.json (no new answers are
generated), scores a fixed subset of cases with both frameworks using the same
judge model, and writes artifacts/framework_comparison.json.

Usage:  python scripts/compare_frameworks.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
CASE_IDS = ["E04", "M01", "M04", "H01", "H04", "A01", "A02"]
METRICS = ["faithfulness", "answer_relevancy", "context_recall", "context_precision"]

load_dotenv(ROOT / ".env")
JUDGE_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_BASE_URL = os.getenv(
    "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/"
)
REQUEST_PAUSE_SECONDS = 8
HF_TOKEN = os.getenv("HF_TOKEN", "")
EMBEDDING_MODEL = os.getenv(
    "HF_EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
)


def _hf_embeddings():
    """LangChain Embeddings backed by the Hugging Face Inference API.

    Gemini's OpenAI-compatible embeddings endpoint failed silently inside RAGAS
    (AnswerRelevancy came back NaN), so embeddings go through HF instead.
    """
    from huggingface_hub import InferenceClient
    from langchain_core.embeddings import Embeddings

    client = InferenceClient(provider="hf-inference", api_key=HF_TOKEN)

    class HFInferenceEmbeddings(Embeddings):
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            vectors = client.feature_extraction(texts, model=EMBEDDING_MODEL, normalize=True)
            return [list(map(float, v)) for v in vectors]

        def embed_query(self, text: str) -> list[float]:
            return self.embed_documents([text])[0]

    return HFInferenceEmbeddings()


def load_cases() -> list[dict]:
    golden = json.loads((ROOT / "golden_dataset.json").read_text(encoding="utf-8"))
    golden = {q["id"]: q for q in golden["qa_pairs"]}
    answers = json.loads((ROOT / "artifacts/actual_answers.json").read_text(encoding="utf-8"))
    answers = {a["id"]: a for a in answers["answers"]}
    return [
        {
            "id": cid,
            "question": golden[cid]["question"],
            "answer": answers[cid]["actual_answer"],
            "contexts": [c["text"] for c in answers[cid]["retrieved_contexts"]],
            "reference": golden[cid]["expected_answer"],
        }
        for cid in CASE_IDS
    ]


def run_ragas(cases: list[dict], only: list[str] | None = None) -> dict[str, dict[str, float]]:
    from langchain_openai import ChatOpenAI
    from ragas import EvaluationDataset, RunConfig, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        AnswerRelevancy,
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
    )

    # Gemini through its OpenAI-compatible endpoint, same judge as DeepEval.
    llm = LangchainLLMWrapper(
        ChatOpenAI(model=JUDGE_MODEL, temperature=0, api_key=GEMINI_API_KEY, base_url=GEMINI_BASE_URL)
    )
    emb = LangchainEmbeddingsWrapper(_hf_embeddings())
    dataset = EvaluationDataset.from_list(
        [
            {
                "user_input": c["question"],
                "response": c["answer"],
                "retrieved_contexts": c["contexts"],
                "reference": c["reference"],
            }
            for c in cases
        ]
    )
    metrics = [
        Faithfulness(llm=llm),
        # strictness=1: Gemini's OpenAI-compatible API rejects n>1 ("Multiple candidates").
        AnswerRelevancy(llm=llm, embeddings=emb, strictness=1),
        LLMContextRecall(llm=llm),
        LLMContextPrecisionWithReference(llm=llm),
    ]
    columns = {
        "faithfulness": "faithfulness",
        "answer_relevancy": "answer_relevancy",
        "context_recall": "context_recall",
        "context_precision": "llm_context_precision_with_reference",
    }
    if only:
        keep = [i for i, m in enumerate(columns) if m in only]
        metrics = [metrics[i] for i in keep]
        columns = {m: col for m, col in columns.items() if m in only}
    # Free-tier Gemini allows ~15 requests/minute: run serially and retry on 429.
    run_config = RunConfig(max_workers=1, max_retries=15, max_wait=90, timeout=300)
    df = evaluate(dataset, metrics=metrics, run_config=run_config, show_progress=True).to_pandas()
    return {
        c["id"]: {m: float(df.iloc[i][col]) for m, col in columns.items()}
        for i, c in enumerate(cases)
    }


def _measure_with_retry(metric, test_case, attempts: int = 8) -> None:
    """Call metric.measure, backing off when the free-tier quota (429) is hit."""
    for attempt in range(attempts):
        try:
            metric.measure(test_case)
            time.sleep(REQUEST_PAUSE_SECONDS)
            return
        except Exception as exc:  # google.genai ClientError / retry wrappers
            if "429" not in str(exc) and "RESOURCE_EXHAUSTED" not in str(exc):
                raise
            wait = 65
            print(f"  quota hit ({metric.__class__.__name__}), waiting {wait}s [{attempt + 1}/{attempts}]")
            time.sleep(wait)
    raise RuntimeError(f"{metric.__class__.__name__} failed after {attempts} quota retries")


def run_deepeval(cases: list[dict]) -> tuple[dict[str, dict[str, float]], dict[str, str]]:
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
        FaithfulnessMetric,
    )
    from deepeval.models import GeminiModel
    from deepeval.test_case import LLMTestCase

    judge = GeminiModel(model=JUDGE_MODEL, api_key=GEMINI_API_KEY, temperature=0)

    factories = {
        "faithfulness": FaithfulnessMetric,
        "answer_relevancy": AnswerRelevancyMetric,
        "context_recall": ContextualRecallMetric,
        "context_precision": ContextualPrecisionMetric,
    }
    scores: dict[str, dict[str, float]] = {}
    reasons: dict[str, str] = {}
    for c in cases:
        tc = LLMTestCase(
            input=c["question"],
            actual_output=c["answer"],
            retrieval_context=c["contexts"],
            expected_output=c["reference"],
        )
        scores[c["id"]] = {}
        for name, factory in factories.items():
            metric = factory(model=judge, threshold=0.5, include_reason=True, async_mode=False)
            _measure_with_retry(metric, tc)
            scores[c["id"]][name] = float(metric.score)
            if name == "answer_relevancy":
                reasons[c["id"]] = metric.reason or ""
    return scores, reasons


def main() -> int:
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY is missing; fill .env first.", file=sys.stderr)
        return 1
    cases = load_cases()
    if "--ragas-relevancy-only" in sys.argv:
        # Re-score only RAGAS AnswerRelevancy and patch the saved comparison.
        out_path = ROOT / "artifacts/framework_comparison.json"
        out = json.loads(out_path.read_text(encoding="utf-8"))
        t0 = time.time()
        scores = run_ragas(cases, only=["answer_relevancy"])
        for cid, s in scores.items():
            out["ragas"][cid]["answer_relevancy"] = s["answer_relevancy"]
            print(f"{cid:<4} ragas answer_relevancy {s['answer_relevancy']:.3f}")
        out["ragas_embedding_model"] = f"{EMBEDDING_MODEL} (HF Inference API)"
        out["runtime_seconds"]["ragas_relevancy_rerun"] = round(time.time() - t0, 1)
        out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0
    bench =json.loads((ROOT / "artifacts/benchmark_results.json").read_text(encoding="utf-8"))
    heuristic = {
        r["id"]: {
            "faithfulness": r["faithfulness"],
            "answer_relevancy": r["relevance"],
            "context_recall": r["context_recall"],
            "context_precision": r["context_precision"],
        }
        for r in bench["results"]
        if r["id"] in CASE_IDS
    }

    t0 = time.time()
    ragas_scores = run_ragas(cases)
    t_ragas = time.time() - t0
    t0 = time.time()
    deepeval_scores, deepeval_reasons = run_deepeval(cases)
    t_deepeval = time.time() - t0

    out = {
        "judge_model": JUDGE_MODEL,
        "ragas_embedding_model": f"{EMBEDDING_MODEL} (HF Inference API)",
        "case_ids": CASE_IDS,
        "runtime_seconds": {"ragas": round(t_ragas, 1), "deepeval": round(t_deepeval, 1)},
        "heuristic": heuristic,
        "ragas": ragas_scores,
        "deepeval": deepeval_scores,
        "deepeval_relevancy_reasons": deepeval_reasons,
    }
    (ROOT / "artifacts/framework_comparison.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    header = f"{'ID':<4} {'metric':<18} {'heur':>6} {'ragas':>6} {'deepev':>6}"
    print(header)
    for cid in CASE_IDS:
        for m in METRICS:
            print(
                f"{cid:<4} {m:<18} {heuristic[cid][m]:>6.2f} "
                f"{ragas_scores[cid][m]:>6.2f} {deepeval_scores[cid][m]:>6.2f}"
            )
    print(f"\nRuntime: RAGAS {t_ragas:.1f}s, DeepEval {t_deepeval:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
