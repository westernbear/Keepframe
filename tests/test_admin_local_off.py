import json
from tests.test_web_server import start, get


def test_admin_still_404_by_default(tmp_path):
    srv = start(tmp_path)
    code, _, body = get(srv, "/admin")
    srv.shutdown()
    assert code == 404
    assert json.loads(body)["error"] == "로컬판에는 이 화면이 없습니다."
