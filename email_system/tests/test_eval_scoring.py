from app.eval.scoring import score_extraction


def test_scoring_any_of_null_and_missing():
    exp = {
        "department": ["CSE", "Computer Science"],
        "budget": None,
        "items_count": 1,
        "quantities": [None],
        "missing_includes": ["budget"],
    }
    data = {
        "department": "Computer Science & Engg",
        "budget": None,
        "items": [{"name": "x", "quantity": None}],
    }
    checks = score_extraction(exp, "requirement", data, ["budget", "quantity"])
    assert all(ok for _, ok, _ in checks), checks


def test_scoring_detects_invented_and_false_missing():
    exp = {"total": None, "missing_required": []}
    checks = dict(
        (n, ok)
        for n, ok, _ in score_extraction(exp, "quotation", {"total": "INR 43,000"}, ["vendor"])
    )
    assert checks == {"total": False, "no_false_missing": False}
