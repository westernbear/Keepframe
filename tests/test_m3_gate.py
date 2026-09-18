from keepframe.gates import m3_gate


def test_m3_gate_small(tmp_path):
    res = m3_gate(tmp_path, n=2)
    assert res["n"] == 2
    assert res["done"] == 2
    assert res["passed"] is True
    assert len(res["rows"]) == 2
    for row in res["rows"]:
        assert row["status"] == "done"
        assert row["keep"] >= 0.95
        assert row["temporal"] >= 0.7
