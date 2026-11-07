"""What a judge reads must be true (audit-1107 mistakes #7, #9, #11, #14; bugs B2, B4). Source greps: there is no browser
in this suite, so these pin the words, not the rendering. run.check.mjs pins the UI's pure helpers."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def src(name):
    return (WEB / "src" / name).read_text(encoding="utf-8")


def test_headings_do_not_claim_vultr():
    app = src("App.jsx")
    assert "Vultr ledger" not in app and "Containment and Vultr" not in app


def test_no_challenge_1_pitch():
    assert "Blast radius" not in src("Landing.jsx")
    product = (ROOT / "PRODUCT.md").read_text(encoding="utf-8")
    assert "Track 1" not in product and "Blast Radius" not in product
    assert "Powering the Future of Work" in product


def test_no_live_model_wording_lives_behind_one_flag():
    assert "VITE_DRYDOCK_LIVE" in src("api.js")
    for f in [WEB / "index.html", *(p for p in (WEB / "src").iterdir() if p.name not in ("Landing.jsx", "fixtures.json"))]:
        assert "No live model" not in f.read_text(encoding="utf-8"), f
    landing = src("Landing.jsx")
    assert re.search(r"LIVE\s*\?[^:]*:\s*'[^']*No live model", landing)
    assert "no live model" not in (WEB / "index.html").read_text(encoding="utf-8").lower()


def test_landing_reaches_the_live_app_when_live():
    landing = src("Landing.jsx")
    assert re.search(r"CTA = LIVE\s*\?\s*\{ href: '\?app'", landing)
    assert "?fixture" in landing  # samples stay reachable


def test_nothing_claims_a_catch_that_never_happened():
    landing = src("Landing.jsx")
    assert "Caught before approval" not in landing and "Scripted example" in landing
    assert "hand-written test SQL" in landing


def test_role_wording_claims_no_chooser():
    """harsh F7: no role picker exists, the class picks the role. Nothing may say the visitor chose it."""
    assert "role chosen by you" not in src("App.jsx") and "no login" in src("App.jsx")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "picked in the browser" not in readme and "approver_role_asserted" in readme


def test_landing_does_not_overclaim_grading():
    """harsh F8: the model writes the promise and its own checks, so "AI can't grade itself" overclaims."""
    landing = src("Landing.jsx")
    assert "grade itself" not in landing and "Written by AI, graded by code" in landing


def test_midrun_failure_never_says_no_run_started():
    """e2e H2: the model can fail after the run started. "No run started" is then false."""
    app = src("App.jsx")
    assert "error.started" in app and "The run stopped partway" in app
    assert "catch (e) { e.started = true; throw e }" in src("api.js")


def test_stale_task_end_wording_is_gone():
    assert "Close and reset" not in src("App.jsx")


def test_working_screen_does_not_promise_a_minute():
    assert "Seconds to a minute" not in src("App.jsx")


def test_fixtures_claim_no_vultr_service_and_e11_is_one_attempt():
    fx = json.loads(src("fixtures.json"))
    text = json.dumps(fx)
    assert "Vultr Serverless Inference" not in text and "on the Vultr VM" not in text and "Vultr Cloud Compute" not in text
    e11 = [s for s in fx["runs"]["E11"]["trace"]["steps"] if s["name"] == "compare"]
    assert len(e11) == 1 and fx["runs"]["E11"]["trace"]["revisions"] == 0


def test_e11_copy_matches_one_stopped_attempt():
    landing = src("Landing.jsx")
    assert "All {E11_ATTEMPTS.length} attempts" not in landing and "first attempt" in landing


def test_vultr_inference_label_only_where_the_host_is_checked():
    for p in ["orch/runs.py", "orch/app.py", "llm/client.py", "eval/run_eval.py", "docs/api.md"]:
        assert "Vultr Serverless Inference" not in (ROOT / p).read_text(encoding="utf-8"), p


def test_env_example_has_neutral_names_and_no_default_host():
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "VULTR_INFERENCE" not in env and "DRYDOCK_VM_ID=" in env
    assert re.search(r"^LLM_BASE_URL=$", env, re.M) and re.search(r"^LLM_API_KEY=$", env, re.M)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_ui_helpers():
    r = subprocess.run(["node", str(WEB / "src" / "run.check.mjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]


def test_fixture_never_turns_free_text_into_a_replayed_green():
    """e2e H4: typed text in ?fixture went green and "Approved and applied" on E2's replay. Samples cover listed tickets only."""
    api = src("api.js")
    assert "FX.runs.E2" not in api and "fx-text" not in api
    assert re.search(r"if \(!input\.ticket_id\) throw new ApiError\(422, 'sample_only'", api)
    assert "{data && !FIXTURE && (" in src("App.jsx")  # no free-text form on the sample inbox
    assert "Samples cover only the tickets above" in src("App.jsx")


def test_failure_screens_offer_the_samples():
    """e2e H3: "Model unavailable" had no way out but the inbox, where every ticket fails the same way."""
    app = src("App.jsx")
    failure = app[app.index("function Failure"):app.index("function Working")]
    assert 'href="?fixture"' in failure and "sandbox_down" in failure


def test_readme_setup_builds_the_site_and_names_both_urls():
    """e2e M6: uvicorn alone served a JSON 404 at /, and ?app was only in Status."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    setup = readme[readme.index("## Setup"):readme.index("Deploy to a fresh")]
    assert "npm run build" in setup and "localhost:8000/?app" in setup and "?fixture" in setup


def test_main_reading_has_no_dev_jargon():
    """design 1, 5, 6; harsh F21: the app speaks the landing's words; technical terms stay in disclosures."""
    app, ui = src("App.jsx"), src("ui.jsx")
    for word in ("Pattern A", "Fixture", "Lookup:", "Now violating", "ARR at risk", "Class ${", "checksum before"):
        assert word not in app + ui, word
    assert "run.ticket.body" in app and "<summary>The ticket</summary>" not in app
    assert "['E8', 'E11']" in app  # the two runs that show the product come first


def test_e8_green_says_why_the_promise_moved():
    """judge Q3, design 4: a moved promise must not read as the AI lowering its own bar."""
    assert "revisedLine(prev, attempt)" in src("ui.jsx") and "prev={attempts[view - 1]}" in src("App.jsx")
    assert "new promise: green" not in src("Landing.jsx")


def test_live_header_names_the_model_from_the_server():
    """With a live model behind the site, the header says which one, read from /api/health, not typed into the page."""
    landing = src("Landing.jsx")
    assert "fetch('/api/health')" in landing and "Live runs on {liveModel}" in landing
    assert "glm" not in landing.lower()
