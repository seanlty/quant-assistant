from src.web_dashboard import get_finmind_token, get_fugle_token, render_dashboard_shell


def test_web_dashboard_reads_api_keys(monkeypatch):
    monkeypatch.setenv("FINMIND_API_TOKEN", "finmind-token")
    monkeypatch.setenv("FUGLE_API_KEY", "fugle-key")

    assert get_finmind_token() == "finmind-token"
    assert get_fugle_token() == "fugle-key"


def test_web_dashboard_shell_renders_api_bootstrap():
    html = render_dashboard_shell()

    assert "/api/pool" in html
