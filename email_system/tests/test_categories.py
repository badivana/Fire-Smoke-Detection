import pytest
import yaml
from pydantic import ValidationError

from app.core.categories import CategoryConfig, ExtractionSchema, get_categories, load_categories

REQUIRED = {
    "REQUIREMENT",
    "VENDOR_QUOTATION",
    "INVOICE",
    "TECHNICAL_QUERY",
    "GENERAL",
    "IRRELEVANT",
}


def test_shipped_config_has_all_required_categories():
    cfg = get_categories()
    assert REQUIRED <= set(cfg.names)
    assert cfg.get("REQUIREMENT").extraction_schema == ExtractionSchema.REQUIREMENT
    assert cfg.get("VENDOR_QUOTATION").extraction_schema == ExtractionSchema.QUOTATION


def test_spam_is_never_auto_drafted():
    cfg = get_categories()
    irr = cfg.get("IRRELEVANT")
    assert irr.draft_reply is False
    assert irr.requires_action is False


def test_unknown_llm_category_falls_back_and_is_flagged():
    cfg = get_categories()
    cat, unknown = cfg.resolve("MADE_UP")
    assert cat.name == "GENERAL" and unknown is True
    cat, unknown = cfg.resolve(" invoice ")
    assert cat.name == "INVOICE" and unknown is False
    _, unknown = cfg.resolve(None)
    assert unknown is True


def test_injection_phrases_configured_lowercase():
    phrases = get_categories().spam_signals.suspicious_phrases
    assert "ignore previous instructions" in phrases
    assert all(p == p.lower() for p in phrases)


def _base():
    return {
        "fallback_category": "GENERAL",
        "categories": [
            {"name": "GENERAL", "description": "General institutional mail."},
            {"name": "IRRELEVANT", "description": "Spam and marketing.", "draft_reply": False},
        ],
    }


def test_new_category_needs_no_code(tmp_path):
    data = _base()
    data["categories"].append(
        {
            "name": "HR_REQUEST",
            "description": "Staff HR related requests.",
            "default_priority": "HIGH",
        }
    )
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(data))
    cfg = load_categories(p)
    assert "HR_REQUEST" in cfg.names
    assert cfg.resolve("hr_request")[0].default_priority == "HIGH"


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda d: d["categories"].append(dict(d["categories"][0])), "duplicate"),
        (lambda d: d.update(fallback_category="NOPE"), "not defined"),
        (lambda d: d["categories"][1].update(draft_reply=True), "spam bucket"),
        (lambda d: d.update(fallback_category="IRRELEVANT"), "must not be a no-draft"),
        (lambda d: d["categories"][0].update(name="lower_case"), "UPPER_SNAKE_CASE"),
        (lambda d: d["categories"][0].update(extraction_schema="bogus"), "extraction_schema"),
        (lambda d: d["categories"][0].update(typo_field=1), "typo_field"),
    ],
)
def test_invalid_configs_are_rejected(mutate, msg):
    data = _base()
    mutate(data)
    with pytest.raises(ValidationError, match=msg):
        CategoryConfig.model_validate(data)
