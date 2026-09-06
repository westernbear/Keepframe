import ast
from pathlib import Path
from refstudio.admin.policy import RETRY_CAP, ASSET_GEN_CAP, policy_note, remaining_retries

def test_caps_are_fixed():
    assert RETRY_CAP == 4 and ASSET_GEN_CAP == 2
    assert policy_note() == "재시도 상한 4회 · 에셋 생성 2회 · 관리자가 올릴 수 없음."
    assert remaining_retries(4) == 0
    assert remaining_retries(1) == 3

def test_policy_module_has_no_setter():
    src = (Path("refstudio/admin/policy.py")).read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    assert "set_retry_cap" not in names
    assert "set_asset_cap" not in names
