from app.ingestion.normalize import clean_line, clean_text, for_matching, html_to_text, truncate


def test_html_to_text_drops_scripts_and_keeps_structure():
    r = html_to_text(
        "<html><head><title>T</title><style>p{}</style></head><body>"
        "<p>Hello&nbsp;<b>team</b> &amp; all</p><script>alert(1)</script>"
        "<p>Line two<br>Line three</p><img src=x></body></html>"
    )
    text = clean_text(r.text).text
    assert text == "Hello team & all\n\nLine two\nLine three"
    assert "alert" not in text and r.hidden_text == ""


def test_css_hidden_text_is_separated_not_dropped():
    r = html_to_text(
        '<p>Visible</p><div style="DISPLAY: none">secret instructions</div>'
        '<span style="font-size:0px">tiny</span><p hidden>attr hidden</p>'
        '<span style="opacity:0.5">half visible</span>'
    )
    assert "secret" not in r.text and "tiny" not in r.text and "attr hidden" not in r.text
    assert "half visible" in r.text  # opacity 0.5 is visible
    assert r.hidden_text == "secret instructions tiny attr hidden"


def test_unclosed_and_nested_tags_do_not_leak_hidden_state():
    r = html_to_text('<div style="display:none"><p>a</p><br>b</div><p>shown')
    assert r.hidden_text == "a b"
    assert "shown" in r.text


def test_invisible_and_control_chars_removed_and_counted():
    r = clean_text("\ufeffig\u200bnore\u202e pre\x00vious\r\nline\n\n\n\nend  ")
    assert r.text == "ignore previous\nline\n\nend"
    assert r.invisible_chars_removed == 2  # leading BOM not counted


def test_nfkc_folds_lookalike_letters():
    assert (
        for_matching("\uff29\uff27\uff2e\uff2f\uff32\uff25   previous\ninstructions")
        == "ignore previous instructions"
    )


def test_clean_line_single_line_and_capped():
    assert clean_line("  Re:\n  Quote\t\tfor  PCs ", 100) == "Re: Quote for PCs"
    assert len(clean_line("x" * 5000, 998)) == 998


def test_truncate_marks_cut():
    text = "\n".join(f"line {i}" for i in range(1000))
    out, cut = truncate(text, 500)
    assert cut and "[... truncated" in out and len(out) < 560
    assert truncate("short", 500) == ("short", False)
