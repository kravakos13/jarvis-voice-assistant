"""'Джарвис, проверь себя' — self-diagnostics by voice; 'выключи микрофон' privacy pause."""
from __future__ import annotations

import re
import threading
from typing import Optional

from .base import Skill, ToolResult

DIAG_RE = re.compile(r"\b(проверь себя|самодиагностик\w*|диагностик\w*|проверь систем\w*|всё ли работает|все ли работает)\b")
SYNC_RE = re.compile(r"^(синхронизируй\w*|синхронизация|сделай синхронизацию|синхронизируй(ся)? (всё|все|заметки|данные))$")
COLLECT_RE = re.compile(r"^(?:(?:джарвис|ну|давай|пожалуйста)\s+)*(?:собери|сохрани|сделай|выгрузи|запиши|сложи)\s+"
                        r"(?:мне\s+)?(?:диагностик\w*|логи|лог|отчет для поддержки|архив диагностики)"
                        r"(?:\s+на\s+рабочий\s+стол)?$")
MIC_OFF_RE = re.compile(r"^(выключи|отключи|выруби)\s+(микрофон|микро|прослушку)$|^не слушай$|^перестань слушать$")


class DiagSkill(Skill):
    name = "diag"

    def match(self, text: str, t: str) -> Optional[ToolResult]:
        a = self.ctx.extra.get("assistant")
        if MIC_OFF_RE.match(t) and a:
            hk = self.ctx.cfg.get("gui", {}).get("hotkey_mic", "ctrl+alt+m")
            threading.Timer(1.5, lambda: a.set_mic_muted(True)).start()   # after the reply is spoken
            return ToolResult(f"Микрофон выключен. Включить — {hk.replace('+', ' плюс ')} или через трей.")
        if SYNC_RE.match(t):
            def run_sync():
                r = a.sync_now() if a else None
                self.ctx.say(r.message if r else "Синхронизация недоступна.", "normal")
            threading.Thread(target=run_sync, daemon=True).start()
            return ToolResult("Синхронизирую.")
        if COLLECT_RE.match(t.replace("ё", "е")):
            def pack():
                from .. import diagbundle
                try:
                    path = diagbundle.collect(self.ctx.cfg, a)
                    if a:
                        a.emit("notice", text=f"Диагностика: {path}")
                    self.ctx.say(f"Готово: архив «{path.name}» на рабочем столе.", "normal")
                except Exception as e:                   # noqa: BLE001
                    self.ctx.say(f"Не получилось собрать диагностику: {e}", "normal")
            threading.Thread(target=pack, name="jarvis-diag-zip", daemon=True).start()
            return ToolResult("Собираю логи и сведения о видеокарте в архив.")
        if DIAG_RE.search(t):
            def work():
                from .. import diagnostics
                checks = diagnostics.run(self.ctx.cfg, a, quick=False)
                if a:
                    a.emit("diagnostics", checks=[c.__dict__ for c in checks])
                self.ctx.say(diagnostics.spoken_summary(checks), "normal")
            threading.Thread(target=work, daemon=True).start()
            return ToolResult("Запускаю самодиагностику.")
        return None
