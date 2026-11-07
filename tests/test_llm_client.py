"""Task 4: LLM client + eval runner. Offline: httpx.MockTransport, no network, no key, no Docker."""
import json
import subprocess
from types import SimpleNamespace

import httpx
import pytest

import sandbox.container as container
from core.contract import Failure, Verdict
from eval import run_eval
from llm import client
from core.measure import MoneyDelta
from llm.client import ModelRequestError, ModelTruncated, ModelUnavailable, chat

CANNED = {
    "id": "chatcmpl-1", "model": "glm-5.3-flash",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "{\"tables\": [\"invoices\"]}"}}],
    "usage": {"prompt_tokens": 812, "completion_tokens": 64, "total_tokens": 876},
}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "sk-test-secret-123")
    monkeypatch.setenv("VULTR_INFERENCE_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("DRYDOCK_MODEL", "glm-5.3-flash")


def transport(handler):
    return httpx.MockTransport(handler)


def ok_handler(seen):
    def h(req):
        seen.append(req)
        return httpx.Response(200, json=CANNED)
    return h


def test_recorded_response_parses(env):
    seen = []
    r = chat([{"role": "user", "content": "hi"}], transport=transport(ok_handler(seen)))
    assert r.text == "{\"tables\": [\"invoices\"]}"
    assert r.model_id == "glm-5.3-flash"
    assert (r.tokens_in, r.tokens_out) == (812, 64)
    assert r.ms >= 0
    assert r.message["role"] == "assistant"


def test_request_shape(env):
    seen = []
    chat([{"role": "user", "content": "hi"}], max_tokens=300, tools=[{"type": "function"}],
         transport=transport(ok_handler(seen)))
    req = seen[0]
    assert str(req.url) == "https://example.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test-secret-123"
    body = json.loads(req.content)
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 300
    assert body["model"] == "glm-5.3-flash"
    assert body["tools"] == [{"type": "function"}]
    assert "response_format" not in body  # only sent when asked


def test_default_max_tokens_sent(env):
    seen = []
    chat([{"role": "user", "content": "hi"}], transport=transport(ok_handler(seen)))
    assert json.loads(seen[0].content)["max_tokens"] == client.MAX_TOKENS


@pytest.mark.parametrize("value,sent", [(None, "medium"), ("low", "low"), ("", None)])
def test_reasoning_effort(env, monkeypatch, value, sent):
    # live 2026-11-07: glm-5.3-flash spent all 8192 tokens reasoning on E8's revise contract (content empty)
    if value is None:
        monkeypatch.delenv("DRYDOCK_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("DRYDOCK_REASONING_EFFORT", value)
    seen = []
    chat([{"role": "user", "content": "hi"}], transport=transport(ok_handler(seen)))
    assert json.loads(seen[0].content).get("reasoning_effort") == sent


def test_missing_key_is_model_unavailable(monkeypatch):
    monkeypatch.delenv("VULTR_INFERENCE_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    called = []
    with pytest.raises(ModelUnavailable, match="Model unavailable"):
        chat([{"role": "user", "content": "hi"}], transport=transport(ok_handler(called)))
    assert called == []  # no request without a key


def test_timeout_is_model_unavailable(env):
    def h(req):
        raise httpx.ReadTimeout("slow", request=req)
    with pytest.raises(ModelUnavailable, match="Model unavailable"):
        chat([{"role": "user", "content": "hi"}], transport=transport(h))


@pytest.mark.parametrize("status", [402, 429, 500, 503])
def test_http_error_is_model_unavailable(env, status):
    with pytest.raises(ModelUnavailable) as e:
        chat([{"role": "user", "content": "hi"}],
             transport=transport(lambda req: httpx.Response(status, json={"error": {"message": "x"}})))
    assert str(status) in e.value.detail


@pytest.mark.parametrize("status", [400, 401, 404, 422])
def test_other_4xx_is_request_error_not_unavailable(env, status):  # review-1105 B2: our bug, not an outage
    with pytest.raises(ModelRequestError) as e:
        chat([{"role": "user", "content": "hi"}],
             transport=transport(lambda req: httpx.Response(status, json={"error": {"message": "x"}})))
    assert not isinstance(e.value, ModelUnavailable) and str(status) in e.value.detail


def test_public_error_text_is_fixed_and_safe(env):  # review-1105 wow#1: no raw provider body on the card
    body = '{"error":"Please fund your account"}'
    for status in (402, 401):
        with pytest.raises((ModelUnavailable, ModelRequestError)) as e:
            chat([{"role": "user", "content": "hi"}], transport=transport(lambda req: httpx.Response(status, text=body)))
        assert str(e.value) == e.value.public == "Model unavailable, try later"
        assert "fund" in e.value.detail and "HTTP" in e.value.detail  # detail is for logs / ledger only


def test_truncated_reply_is_distinct(env):  # review-1105 H5/B3
    cut = json.loads(json.dumps(CANNED))
    cut["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ModelTruncated) as e:
        chat([{"role": "user", "content": "hi"}], transport=transport(lambda req: httpx.Response(200, json=cut)))
    assert not isinstance(e.value, ModelUnavailable) and e.value.reply.tokens_out == 64


def test_finish_reason_on_reply(env):
    r = chat([{"role": "user", "content": "hi"}], transport=transport(ok_handler([])))
    assert r.finish_reason == "stop"


def test_error_never_leaks_key(env):
    with pytest.raises(ModelRequestError) as e:
        chat([{"role": "user", "content": "hi"}],
             transport=transport(lambda req: httpx.Response(401, text="bad key sk-test-secret-123")))
    assert "sk-test-secret-123" not in str(e.value) and "sk-test-secret-123" not in e.value.detail


def test_smoke_prints_model_and_usage(env, capsys):
    from llm import smoke
    assert smoke.main(transport=transport(ok_handler([]))) == 0
    out = capsys.readouterr().out
    assert "glm-5.3-flash" in out and "812" in out and "64" in out


def test_smoke_without_key_exits_nonzero(monkeypatch, capsys):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    from llm import smoke
    monkeypatch.delenv("VULTR_INFERENCE_API_KEY", raising=False)
    assert smoke.main() == 1
    assert "Model unavailable" in capsys.readouterr().out


def test_env_file_loader(tmp_path, monkeypatch, capsys):  # review-1105 B6: stdlib, no override, prints nothing
    import os
    f = tmp_path / ".env"
    f.write_text('# names\nDRYDOCK_T1=from-file\nDRYDOCK_T2 = "quoted"\nDRYDOCK_T3=keep-env\n\nnot a line\n',
                 encoding="utf-8")
    monkeypatch.delenv("DRYDOCK_T1", raising=False)
    monkeypatch.delenv("DRYDOCK_T2", raising=False)
    monkeypatch.setenv("DRYDOCK_T3", "env-wins")
    client.load_env(f)
    assert (os.environ["DRYDOCK_T1"], os.environ["DRYDOCK_T2"], os.environ["DRYDOCK_T3"]) == \
        ("from-file", "quoted", "env-wins")
    monkeypatch.delenv("DRYDOCK_T1")
    monkeypatch.delenv("DRYDOCK_T2")
    assert capsys.readouterr().out == ""
    client.load_env(tmp_path / "missing.env")  # no file: no-op


def test_key_never_reaches_sandbox_container(env, monkeypatch):
    calls = []

    def fake_docker(*args, input=None, timeout=60):
        calls.append(list(args))
        return subprocess.CompletedProcess(["docker", *args], 0, "", "")
    monkeypatch.setattr(container, "docker", fake_docker)
    container.start_run_container()
    n = len(calls)
    container.recreate_live()  # the live-db container too (judge Q7)
    assert any(a and a[0] == "run" for a in calls[n:]), "recreate_live issued no docker run"
    runs = [a for a in calls if a and a[0] in ("run", "create", "exec")]
    assert runs, "start_run_container issued no docker run"
    for a in runs:
        assert not {"-e", "--env", "--env-file"} & set(a), a
        assert not any(x.startswith(("--env=", "--env-file=")) for x in a), a
        assert not any("sk-test-secret-123" in x or "VULTR_INFERENCE" in x or "LLM_" in x for x in a), a


# ---------- eval runner ----------

def _money(cents):
    return {"x.amount_cents": MoneyDelta(0, cents, cents, True, 1, "", headline=True)}


def _effect(rows_by_table, cents=0):
    return SimpleNamespace(rows_by_table=rows_by_table, money_cents_by_column=_money(cents),
                           changed_count=lambda t: rows_by_table.get(t, {}).get("touched", 0))


def _step(name, tokens=0, ms=0, payload=None):
    return SimpleNamespace(name=name, tokens=tokens, ms=ms, payload=payload, model_id="glm-5.3-flash")


def _trace(golden, *, green=True, revisions=0, verdicts=None, steps=None, explanation="", rows=None, cents=None):
    v = Verdict(green, [] if green else [Failure("check", "x did not return true")])
    return SimpleNamespace(verdict=v, verdicts=verdicts or [v], revisions=revisions,
                           effect=_effect(golden["expected"] if rows is None else rows,
                                          golden["expected_amount_cents"] if cents is None else cents),
                           steps=steps or [_step("contract", 100, 40), _step("sql", 50, 20)],
                           explanation=explanation)


G = run_eval.load_goldens()


def test_score_pass_when_effect_equals_golden():
    assert run_eval.score("E2", G["E2"], _trace(G["E2"]))["passed"]


def test_score_fail_on_wrong_effect():
    s = run_eval.score("E2", G["E2"], _trace(G["E2"], rows={"accounts": {"count_delta": 0, "touched": 263}}))
    assert not s["passed"]


def test_score_fail_on_wrong_amount():  # review-1105 H1: right row count, wrong money = fail
    g = G["E6"]
    assert run_eval.score("E6", g, _trace(g))["passed"]
    assert not run_eval.score("E6", g, _trace(g, cents=g["expected_amount_cents"] * 2))["passed"]  # 20%, not 10%


def test_score_tokens_from_reply_fields():  # review-1105 H6: Reply-shaped steps (tokens_in/out) still count
    st = SimpleNamespace(name="contract", tokens_in=800, tokens_out=76, ms=5, payload={}, model_id="m")
    assert run_eval.score("E2", G["E2"], _trace(G["E2"], steps=[st]))["tokens"] == 876


def test_score_fail_on_red_or_too_many_revisions():
    assert not run_eval.score("E2", G["E2"], _trace(G["E2"], green=False))["passed"]
    assert not run_eval.score("E2", G["E2"], _trace(G["E2"], revisions=3))["passed"]


def test_score_e8_first_attempt_p7_hit():
    red = Verdict(False, [Failure("policy", "P-7: violations 0 → 3")])
    s = run_eval.score("E8", G["E8"], _trace(G["E8"], revisions=1, verdicts=[red, Verdict(True)]))
    assert s["passed"] and s["p7_first"] is True


def _e11_trace(code, explanation):
    stopped = SimpleNamespace(ok=False, code=code, error="cap")
    red = Verdict(False, [Failure("cap", "stopped by disk spill cap")])
    return SimpleNamespace(verdict=red, verdicts=[red], revisions=2, effect=_effect({}),
                           steps=[_step("contract", 10, 5), _step("run", payload=stopped)],
                           explanation=explanation)


def test_score_e11_cap_fired_class_c_explained():
    s = run_eval.score("E11", G["E11"], _e11_trace("53400", "Stopped by the disk spill cap; needs a batched job."))
    assert s["passed"] and s["cap"] == "53400"


def test_score_e11_fails_without_cap_or_explanation():
    assert not run_eval.score("E11", G["E11"], _e11_trace(None, "disk spill cap"))["passed"]
    assert not run_eval.score("E11", G["E11"], _e11_trace("53400", "done"))["passed"]


def test_score_e11_accepts_any_name_of_the_fired_cap():  # review-1105 wow#3/M5: plan says "explanation correct"
    for words in ("Stopped by temp_file_limit.", "hit the disk spill cap", "SQLSTATE 53400"):
        assert run_eval.score("E11", G["E11"], _e11_trace("53400", words))["passed"], words
    assert not run_eval.score("E11", G["E11"], _e11_trace("53400", "stopped by statement_timeout"))["passed"]


def test_cap_only_counts_on_run_steps():  # review-1105 M6
    t = _e11_trace("53400", "disk spill cap")
    t.steps[1].name = "lookup"
    assert run_eval.score("E11", G["E11"], t)["cap"] is None


def test_contract_validity_counts_errors():
    t = _trace(G["E2"], steps=[_step("contract", payload={"error": "bad json"}), _step("contract", payload={})])
    s = run_eval.score("E2", G["E2"], t)
    assert (s["contracts"], s["contracts_valid"]) == (2, 1)


def test_gate():
    good = {k: True for k in run_eval.DEMO}
    one_miss = dict(good, E5=False)
    two_miss = dict(good, E5=False, E6=False)
    fired = [True, True, True]
    assert run_eval.gate([good, one_miss, good], fired)
    assert not run_eval.gate([good, two_miss, good], fired)
    assert not run_eval.gate([good, good, good], [True, True, False])  # E11 cap must fire 3/3 (plan §2)
    assert run_eval.gate([good, good, dict(good, E11=False)], fired)  # E11 explanation miss = 1 of 6, not a gate fail


def test_run_writes_results_with_score_line(env, tmp_path):
    """Fake agent drives the real client through MockTransport, so tokens/model id come from the client."""
    t = transport(ok_handler([]))

    def agent(ticket, box):
        r = chat([{"role": "user", "content": ticket["body"]}], transport=t)
        g = G[ticket["id"]]
        if ticket["id"] == "E11":
            return _e11_trace("53400", "stopped by the disk spill cap")
        return _trace(g, steps=[_step("contract", r.tokens_in + r.tokens_out, r.ms, {})])

    res = run_eval.run("glm-5.3-flash", 3, agent=agent, start=lambda: "box", stop=lambda b: None)
    path = run_eval.write_results(res, out_dir=tmp_path)
    md = path.read_text(encoding="utf-8")
    assert "18/18 correct vs hand-written goldens" in md
    assert "glm-5.3-flash" in md and "GATE: PASS" in md and "held-out" in md.lower()
    assert "tokens per run: [7018, 7018, 7018]" in md  # 8 tickets x 876 from the client + E11's 10 (H6)
    assert "served model id: `glm-5.3-flash`" in md  # B5: from the traces, not the CLI argument
    assert path.name.startswith("results-") and path.name.endswith("-glm-5.3-flash.md")  # H4: timestamp + model


def test_results_file_per_model_never_overwrites(tmp_path):  # review-1105 H4
    res = {"model": "a/b c", "prompt_sha": "x", "runs": [], "aborted": "run 1 ticket E1: x"}
    p1 = run_eval.write_results(res, out_dir=tmp_path)
    p2 = run_eval.write_results(dict(res, model="qwen3.8-27b"), out_dir=tmp_path)
    assert p1 != p2 and p1.exists() and p2.exists() and "/" not in p1.name and " " not in p1.name


def test_partial_results_written_on_mid_eval_outage(tmp_path):  # review-1105 B1 / wow#2
    n = []

    def agent(ticket, box):
        n.append(ticket["id"])
        if len(n) == 3:
            raise ModelUnavailable("Model unavailable, try later", detail="HTTP 429: daily cap")
        return _trace(G[ticket["id"]]) if ticket["id"] != "E11" else _e11_trace("53400", "disk spill cap")

    res = run_eval.run("glm-5.3-flash", 3, agent=agent, start=lambda: "box", stop=lambda b: None)
    assert res["aborted"] and "run 1" in res["aborted"] and "E5" in res["aborted"]
    path = run_eval.write_results(res, out_dir=tmp_path)
    md = path.read_text(encoding="utf-8")
    assert "INCOMPLETE" in path.name and "GATE: INCOMPLETE" in md and "HTTP 429" in md
    assert "correct vs hand-written goldens" not in md  # the N5 regex can never pick up a partial score
    assert "| 1 | E1 | PASS" in md and "| 1 | E2 | PASS" in md


def test_run_aborts_when_model_unavailable(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("VULTR_INFERENCE_API_KEY", raising=False)
    stopped = []
    res = run_eval.run("glm-5.3-flash", 1, agent=lambda tk, b: chat([{"role": "user", "content": "x"}]),
                       start=lambda: "box", stop=stopped.append)
    assert res["aborted"] and stopped == ["box"]  # the box is destroyed even when the run aborts


def test_request_error_and_truncation_are_ticket_fails_not_aborts(env):  # B2 / H5
    def agent(ticket, box):
        if ticket["id"] == "E1":
            raise ModelRequestError("Model unavailable, try later", detail="HTTP 400: bad tools")
        if ticket["id"] == "E2":
            cut = json.loads(json.dumps(CANNED))
            cut["choices"][0]["finish_reason"] = "length"
            chat([{"role": "user", "content": "x"}], transport=transport(lambda req: httpx.Response(200, json=cut)))
        return _trace(G[ticket["id"]]) if ticket["id"] != "E11" else _e11_trace("53400", "disk spill cap")

    res = run_eval.run("glm-5.3-flash", 1, agent=agent, start=lambda: "box", stop=lambda b: None)
    r = res["runs"][0]
    assert not res["aborted"] and len(r) == 9
    assert "ModelRequestError" in r["E1"]["error"] and "HTTP 400" in r["E1"]["error"]
    assert r["E2"]["error"].startswith("ModelTruncated")


# ---------- provider config, retry, pacing, budget (audit-1107 B1, B13, mistakes #3, #8) ----------
def _rate_limited(seen, then=200, retry_after="7"):
    def h(req):
        seen.append(str(req.url))
        if len(seen) == 1:
            return httpx.Response(429, headers={"retry-after": retry_after}, text="slow down")
        return httpx.Response(then, json=CANNED)
    return h


def test_new_env_names_win_and_old_names_still_work(monkeypatch):
    for k in ("LLM_API_KEY", "LLM_BASE_URL", "VULTR_INFERENCE_API_KEY", "VULTR_INFERENCE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DRYDOCK_MODEL", "m")
    monkeypatch.setenv("VULTR_INFERENCE_API_KEY", "old-key")
    monkeypatch.setenv("VULTR_INFERENCE_BASE_URL", "https://old.test/v1")
    assert (client.providers()[0].key, client.providers()[0].base) == ("old-key", "https://old.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "new-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://new.test/v1")
    assert (client.providers()[0].key, client.providers()[0].base) == ("new-key", "https://new.test/v1")


@pytest.mark.parametrize("missing", ["LLM_BASE_URL", "DRYDOCK_MODEL"])
def test_key_without_base_url_or_model_refuses_and_sends_nothing(monkeypatch, missing):
    for k in ("VULTR_INFERENCE_API_KEY", "VULTR_INFERENCE_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "gsk-groq-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("DRYDOCK_MODEL", "m")
    monkeypatch.delenv(missing)
    seen = []
    with pytest.raises(ModelUnavailable) as e:
        chat([{"role": "user", "content": "x"}], transport=transport(ok_handler(seen)))
    assert seen == [] and missing in e.value.detail
    with pytest.raises(client.ConfigError, match=missing):
        client.validate_env()


def test_validate_env_accepts_no_key_at_all(monkeypatch):
    for k in ("LLM_API_KEY", "VULTR_INFERENCE_API_KEY", "LLM_BASE_URL", "VULTR_INFERENCE_BASE_URL", "DRYDOCK_MODEL"):
        monkeypatch.delenv(k, raising=False)
    client.validate_env()  # the app runs and shows the model-unavailable card


def test_429_retries_and_honours_retry_after(env, monkeypatch):
    waits, seen = [], []
    monkeypatch.setattr(client, "_sleep", waits.append)
    r = chat([{"role": "user", "content": "x"}], transport=transport(_rate_limited(seen)))
    assert r.tokens_in == 812 and len(seen) == 2 and waits == [7.0]


def test_503_backs_off_without_retry_after(env, monkeypatch):
    waits, seen = [], []
    monkeypatch.setattr(client, "_sleep", waits.append)

    def h(req):
        seen.append(1)
        return httpx.Response(503, text="x") if len(seen) < 3 else httpx.Response(200, json=CANNED)
    chat([{"role": "user", "content": "x"}], transport=transport(h))
    assert len(seen) == 3 and len(waits) == 2 and 0 < waits[0] < waits[1]


def test_retry_after_past_the_budget_fails_at_once(env, monkeypatch):
    waits, seen = [], []
    monkeypatch.setattr(client, "_sleep", waits.append)
    with pytest.raises(ModelUnavailable):
        chat([{"role": "user", "content": "x"}], budget=client.RunBudget(retry_s=5),
             transport=transport(_rate_limited(seen, retry_after="2400")))
    assert waits == [] and len(seen) == 1


def test_retry_budget_is_shared_across_calls_in_a_run(env, monkeypatch):
    b = client.RunBudget(retry_s=10)
    chat([{"role": "user", "content": "x"}], budget=b, transport=transport(_rate_limited([], retry_after="7")))
    with pytest.raises(ModelUnavailable):
        chat([{"role": "user", "content": "x"}], budget=b, transport=transport(_rate_limited([], retry_after="7")))


def test_run_deadline_stops_before_a_call(env):
    seen = []
    with pytest.raises(ModelUnavailable) as e:
        chat([{"role": "user", "content": "x"}], budget=client.RunBudget(deadline_s=0),
             transport=transport(ok_handler(seen)))
    assert seen == [] and "time limit" in e.value.public


def test_run_token_budget_stops_before_a_call(env):
    b, seen = client.RunBudget(tokens=900), []
    chat([{"role": "user", "content": "x"}], budget=b, transport=transport(ok_handler(seen)))  # spends 876
    chat([{"role": "user", "content": "x"}], budget=b, transport=transport(ok_handler(seen)))  # spends 876 more
    with pytest.raises(ModelUnavailable) as e:
        chat([{"role": "user", "content": "x"}], budget=b, transport=transport(ok_handler(seen)))
    assert len(seen) == 2 and "token budget" in e.value.public


def test_pacing_waits_when_the_minute_is_full(env, monkeypatch):
    now = [1000.0]
    waits = []
    monkeypatch.setenv("LLM_TPM_LIMIT", "1000")
    monkeypatch.setattr(client, "_now", lambda: now[0])

    def sleep(s):
        waits.append(s)
        now[0] += s
    monkeypatch.setattr(client, "_sleep", sleep)
    t = transport(ok_handler([]))
    chat([{"role": "user", "content": "x"}], transport=t)  # 876 tokens in the window
    assert waits == []
    chat([{"role": "user", "content": "x" * 2000}], transport=t)  # ~500 estimated: over 1000 until the first call ages out
    assert len(waits) == 1 and 59 <= waits[0] <= 61


def test_pacing_wait_longer_than_the_deadline_gives_up(env, monkeypatch):
    now = [1000.0]
    monkeypatch.setenv("LLM_TPM_LIMIT", "1000")
    monkeypatch.setattr(client, "_now", lambda: now[0])
    monkeypatch.setattr(client, "_sleep", lambda s: pytest.fail("must not sleep past the deadline"))
    t = transport(ok_handler([]))
    chat([{"role": "user", "content": "x"}], transport=t)
    with pytest.raises(ModelUnavailable):
        chat([{"role": "user", "content": "x" * 2000}], budget=client.RunBudget(deadline_s=5), transport=t)


def test_fallback_provider_takes_over_after_retries(env, monkeypatch):
    monkeypatch.setenv("BACKUP_KEY", "backup-secret")
    monkeypatch.setenv("LLM_FALLBACKS", "https://backup.test/v1|BACKUP_KEY|backup-model")
    seen = []

    def h(req):
        seen.append((req.url.host, req.headers["authorization"], json.loads(req.content)["model"]))
        if req.url.host == "example.test":
            return httpx.Response(503, text="down")
        return httpx.Response(200, json=CANNED)
    r = chat([{"role": "user", "content": "x"}], transport=transport(h))
    assert seen[-1] == ("backup.test", "Bearer backup-secret", "backup-model")
    assert r.host == "backup.test"
    assert all(a == "Bearer sk-test-secret-123" for host, a, _ in seen if host == "example.test")


def test_fallback_with_no_key_set_is_skipped(env, monkeypatch):
    monkeypatch.delenv("BACKUP_KEY", raising=False)
    monkeypatch.setenv("LLM_FALLBACKS", "https://backup.test/v1|BACKUP_KEY|m")
    assert [p.base for p in client.providers()] == ["https://example.test/v1"]


def test_malformed_fallback_list_refuses(env, monkeypatch):
    monkeypatch.setenv("LLM_FALLBACKS", "https://backup.test/v1|only-two")
    with pytest.raises(client.ConfigError, match="LLM_FALLBACKS"):
        client.validate_env()


def test_bad_shape_400_does_not_fail_over(env, monkeypatch):
    monkeypatch.setenv("BACKUP_KEY", "k")
    monkeypatch.setenv("LLM_FALLBACKS", "https://backup.test/v1|BACKUP_KEY|m")
    seen = []

    def h(req):
        seen.append(req.url.host)
        return httpx.Response(400, text="bad schema")
    with pytest.raises(ModelRequestError):
        chat([{"role": "user", "content": "x"}], transport=transport(h))
    assert seen == ["example.test"]


def test_tokens_today_counts_every_call(env):
    before = client.tokens_today()
    chat([{"role": "user", "content": "x"}], transport=transport(ok_handler([])))
    assert client.tokens_today() == before + 876


# ---------- reduced eval: a quota-sized run must not pass itself off as the gate (mistakes #3) ----------
def _small(path_dir, tickets, runs):
    def agent(ticket, box):
        if ticket["id"] == "E11":
            return _e11_trace("53400", "stopped by the disk spill cap")
        return _trace(G[ticket["id"]])
    res = run_eval.run("m", runs, agent=agent, start=lambda: "box", stop=lambda b: None, tickets=tickets)
    return res, run_eval.write_results(res, out_dir=path_dir).read_text(encoding="utf-8")


def test_reduced_eval_runs_only_the_named_tickets(tmp_path):
    res, md = _small(tmp_path, ["E8", "E11"], 1)
    assert list(res["runs"][0]) == ["E8", "E11"] and len(res["runs"]) == 1
    assert "**2/2 correct vs hand-written goldens** (reduced: E8, E11 x1)" in md


def test_reduced_eval_never_claims_the_gate(tmp_path):
    for tickets, runs in ((["E8", "E11"], 3), (None, 1)):
        res, md = _small(tmp_path, tickets, runs)
        assert "GATE: NOT EVALUATED" in md and "GATE: PASS" not in md and "GATE: FAIL" not in md
        assert run_eval.gate_result(res) is None


def test_reduced_eval_without_e8_or_e11_does_not_crash(tmp_path):
    _, md = _small(tmp_path, ["E1"], 1)
    assert "**1/1 correct" in md and "E11 cap fired" not in md and "E8 first-attempt" not in md


def test_full_eval_still_reports_the_gate(tmp_path):
    res, md = _small(tmp_path, None, 3)
    assert "GATE: PASS" in md and run_eval.gate_result(res) is True and "reduced" not in md


def test_unknown_ticket_is_refused():
    with pytest.raises(ValueError, match="E99"):
        run_eval.run("m", 1, agent=lambda t, b: None, start=lambda: "b", stop=lambda b: None, tickets=["E99"])


def test_max_tokens_env_is_read_at_call_time(env, monkeypatch):  # bugs.md F8: .env loads after llm.client is imported
    monkeypatch.setenv("DRYDOCK_MAX_TOKENS", "1234")
    seen = []
    chat([{"role": "user", "content": "hi"}], transport=transport(ok_handler(seen)))
    assert json.loads(seen[0].content)["max_tokens"] == 1234
