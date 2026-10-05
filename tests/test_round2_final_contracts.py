import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pipeline_has_no_unused_report_metric_imports():
    tree = ast.parse((ROOT / "keepframe/analyze/pipeline.py").read_text())
    imports = {alias.name for node in tree.body if isinstance(node, ast.ImportFrom)
               and node.module == "report" for alias in node.names}
    assert imports == {"write_report"}


def test_adapter_docs_explain_automatic_upload_quota_and_fallback():
    text = (ROOT / "docs/qa/round2/3d-adapter.md").read_text()
    paragraphs = text.split("\n\n")
    assert any(all(term in paragraph for term in (
        "automatic", "KEEPFRAME_ASSET_API_URL", "object crops", "public Hugging Face Space",
        "your account", "ZeroGPU quota", "2 requests per analysis", "quota exhaustion",
        "failures", "still", "fragments", "report message",
    )) for paragraph in paragraphs)
