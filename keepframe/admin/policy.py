RETRY_CAP = 4
ASSET_GEN_CAP = 2

def policy_note() -> str:
    return "재시도 상한 4회 · 에셋 생성 2회 · 관리자가 올릴 수 없음."

def remaining_retries(used: int) -> int:
    return max(0, RETRY_CAP - used)
