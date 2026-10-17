from pathlib import Path

from jarvis.config import load_config


def test_lumo_flow(monkeypatch, caplog):
    from jarvis import lumo
    from jarvis.secrets import get_secret, set_secret
    from jarvis.skills.base import SkillContext
    from jarvis.skills.lumo_skill import LumoSkill
    cfg = load_config(use_local=False)
    said = []
    ctx = SkillContext(cfg=cfg, llm=None, say=lambda t, *a: said.append(t), notify=lambda *a: None, project_root=Path("."))
    sk = LumoSkill(ctx)
    r = sk.match("передай люмо найди билеты", "передай люмо найди билеты")
    assert "не настроен" in r.reply                      # disabled until URL + key
    cfg["lumo"]["webhook_url"] = "https://example.invalid/hook"
    set_secret(lumo.SECRET_NAME, "s3cr3t-key")
    assert get_secret(lumo.SECRET_NAME) == "s3cr3t-key"
    sent = {}

    class Resp:
        status_code = 200
        text = "ok"
    def fake_post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, json=json, headers=headers)
        return Resp()
    monkeypatch.setattr(lumo.requests, "post", fake_post)
    caplog.set_level("DEBUG")
    r = sk.match("Передай Люмо найди дешёвые билеты в Варшаву", "передай люмо найди дешёвые билеты в варшаву")
    assert r.confirm and "Отправить?" in r.confirm.prompt and not sent     # nothing sent before confirmation
    r.confirm.on_yes()
    import time
    for _ in range(50):
        if said:
            break
        time.sleep(0.05)
    assert said[-1] == "Передал Люмо, ответ придёт в чат Grok Bot."
    p = sent["json"]
    assert p["source"] == "jarvis" and p["task"] == "найди дешёвые билеты в Варшаву" and p["attachments"] == []
    assert sent["headers"]["Authorization"] == "Bearer s3cr3t-key" and sent["headers"]["X-Webhook-Key"] == "s3cr3t-key"
    assert "s3cr3t" not in caplog.text                                     # key never logged
    assert sk.match("отправь это люмо", "отправь это люмо") is not None
    assert sk.match("включи музыку", "включи музыку") is None
    set_secret(lumo.SECRET_NAME, "")
