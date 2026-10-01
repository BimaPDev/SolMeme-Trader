from solmeme.cli import enable_browser_source
from solmeme.config import upsert_env


def test_upsert_env_keeps_comments_and_skips_blanks(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# keys\nGMGN_API_KEY=old\nHELIUS_API_KEY=stay\n", encoding="utf-8")
    written = upsert_env(path, {"GMGN_API_KEY": "new", "JUPITER_API_KEY": "", "PUMPPORTAL_API_KEY": "pp"})
    text = path.read_text(encoding="utf-8")
    assert written == ["GMGN_API_KEY", "PUMPPORTAL_API_KEY"]
    assert "GMGN_API_KEY=new" in text
    assert "HELIUS_API_KEY=stay" in text
    assert "# keys" in text
    assert "JUPITER_API_KEY" not in text
    assert "PUMPPORTAL_API_KEY=pp" in text


def test_enable_browser_source_only_flips_that_site():
    text = "  axiom: {enabled: false, headless: true}\n  j7tracker: {enabled: false, headless: true}\n"
    got, on = enable_browser_source(text, "j7tracker")
    assert on
    assert "  j7tracker: {enabled: true, headless: true}" in got
    assert "  axiom: {enabled: false, headless: true}" in got
    again, still = enable_browser_source(got, "j7tracker")
    assert still and again == got
    missing, off = enable_browser_source(text, "nope")
    assert missing == text and not off


def test_upsert_env_rejects_a_multiline_secret(tmp_path):
    path = tmp_path / ".env"
    try:
        upsert_env(path, {"GMGN_API_KEY": "a\nb"})
    except ValueError as exc:
        assert "line break" in str(exc)
    else:
        raise AssertionError("expected ValueError")
    assert not path.exists()
