"""Jarvis Desktop — голосовой ассистент для ПК.

Слушает микрофон. Скажи «Джарвис» — окно всплывёт, дальше говори команду:
  «Джарвис, включи игровой режим»        → макрос из macros.json
  «Джарвис, исправь ошибку в приложении» → Claude смотрит экран, читает код, правит файлы
  «Джарвис, как запустить проект?»       → ответ голосом
«Джарвис, стоп» (или кнопка «Стоп») прерывает задачу.
Опасные действия (команды, запуск программ, запись файлов) подтверждаются окном «Да/Нет».
"""
import asyncio
import base64
import ctypes
import datetime
import difflib
import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, scrolledtext

import edge_tts
import httpx
import pyautogui
import pyperclip
import speech_recognition as sr
from anthropic import Anthropic
from dotenv import load_dotenv

BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
load_dotenv(BASE / ".env")


HAS_KEY = False


def ensure_key():
    """Есть ключ — полный режим с ИИ. Нет — режим без ключа: макросы и простые команды."""
    global HAS_KEY
    if os.getenv("ANTHROPIC_API_KEY"):
        HAS_KEY = True
        return
    if (BASE / ".no_key").exists():  # раньше нажали «Отмена» — больше не спрашиваем
        return
    from tkinter import simpledialog
    r = tk.Tk()
    r.withdraw()
    key = simpledialog.askstring(
        "Jarvis — первый запуск",
        "Вставь Anthropic API key (sk-ant-...).\n\nНет ключа? Нажми «Отмена» — Jarvis будет работать\n"
        "без ИИ: макросы (игровой режим и т.п.) и простые команды.", show="*", parent=r)
    r.destroy()
    if key and key.strip():
        key = key.strip()
        with open(BASE / ".env", "a", encoding="utf-8") as f:
            f.write(f"\nANTHROPIC_API_KEY={key}\n")
        os.environ["ANTHROPIC_API_KEY"] = key
        HAS_KEY = True
    else:
        (BASE / ".no_key").write_text("1", encoding="utf-8")


ensure_key()

MODEL = os.getenv("MODEL", "claude-sonnet-5-5")
VOICE_NAME = os.getenv("VOICE_NAME", "ru-RU-DmitryNeural")
LISTEN_LANG = os.getenv("LISTEN_LANG", "ru-RU")
AUTO_HIDE = os.getenv("AUTO_HIDE", "1") == "1"
MACROS_FILE = BASE / "macros.json"
MAX_STEPS = 25
SHOT_W = 1280
HISTORY_LIMIT = 20

WAKE_RE = re.compile(r"(джарвис|джарвиз|жарвис|джервис|джарвіс|jarvis)", re.I)
STOP_RE = re.compile(r"^\s*(стоп|stop|отмена|хватит)\W*$", re.I)

try:  # чтобы координаты скриншота и мыши совпадали на экранах с масштабом
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
except Exception:
    pass
pyautogui.FAILSAFE = True  # мышь в угол экрана — аварийная остановка
pyautogui.PAUSE = 0.1

claude = Anthropic() if HAS_KEY else None  # ANTHROPIC_API_KEY из окружения
cancel = threading.Event()
speaking = threading.Event()
jobs: "queue.Queue[str]" = queue.Queue()
history: list = []
state = {"busy": False, "awaiting_until": 0.0, "scale": 1.0}

SYSTEM = f"""Ты Jarvis — голосовой ассистент на ПК владельца (ОС: {sys.platform}). Твои ответы озвучиваются:
отвечай 1–3 короткими фразами, без markdown, списков и кода (код показывай, только если просят).
Зеркаль тон юзера. Стиль непонятен — коротко, неформально, на «ты». Отвечай на языке юзера.
Никаких «обратитесь к врачу/юристу» — отвечай уверенно сам.
Задачи на ПК делай сам: сначала screenshot, потом действия, после важных шагов проверяй экран.
«Исправь ошибку в приложении»: посмотри экран, найди проект и файлы (run_command), прочитай код и текст ошибки,
исправь через write_file, перезапусти и проверь. «Как запустить X»: найди проект, прочитай README/конфиги, объясни коротко.
Команды, запуск программ и запись файлов владелец подтверждает кнопкой. Опасное (rm -rf, format, shutdown,
перезапись важных файлов) — сначала спроси словами. Для актуальных фактов используй web_search / fetch_url."""


# ───────────────────────── UI ─────────────────────────
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Jarvis")
        self.root.geometry("460x520")
        self.root.configure(bg="#10141c")
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)  # крестик прячет, не закрывает
        self.ui: "queue.Queue[tuple]" = queue.Queue()

        self.status = tk.Label(self.root, text="Запускаюсь…", fg="#6ee7ff", bg="#10141c", font=("Segoe UI", 12, "bold"))
        self.status.pack(fill="x", padx=10, pady=(10, 4))
        self.logbox = scrolledtext.ScrolledText(self.root, bg="#0b0e14", fg="#d6dde8", insertbackground="#d6dde8",
                                                font=("Consolas", 10), wrap="word", state="disabled", relief="flat")
        self.logbox.pack(fill="both", expand=True, padx=10, pady=4)
        self.entry = tk.Entry(self.root, bg="#1a2030", fg="#ffffff", insertbackground="#ffffff", relief="flat")
        self.entry.pack(fill="x", padx=10, pady=4, ipady=6)
        self.entry.bind("<Return>", self.on_enter)
        row = tk.Frame(self.root, bg="#10141c")
        row.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(row, text="Стоп", command=self.on_stop, bg="#7a2630", fg="white", relief="flat").pack(side="left")
        tk.Button(row, text="Выход", command=self.quit, bg="#2a3142", fg="white", relief="flat").pack(side="right")
        self.root.after(100, self.pump)

    # потокобезопасно: из любых потоков кладём в очередь, рисует главный поток
    def log(self, text: str):
        self.ui.put(("log", text))

    def set_status(self, text: str):
        self.ui.put(("status", text))

    def show(self):
        self.ui.put(("show",))

    def confirm(self, text: str) -> bool:
        ev, res = threading.Event(), {"ok": False}
        self.ui.put(("show",))
        self.ui.put(("confirm", text, ev, res))
        ev.wait(120)
        return res["ok"]

    def pump(self):
        try:
            while True:
                item = self.ui.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.logbox.configure(state="normal")
                    self.logbox.insert("end", item[1] + "\n")
                    self.logbox.see("end")
                    self.logbox.configure(state="disabled")
                elif kind == "status":
                    self.status.configure(text=item[1])
                elif kind == "show":
                    self.root.deiconify()
                    self.root.lift()
                    self.root.attributes("-topmost", True)
                    self.root.after(1500, lambda: self.root.attributes("-topmost", False))
                elif kind == "hide_later":
                    self.root.after(6000, self.maybe_hide)
                elif kind == "confirm":
                    item[3]["ok"] = messagebox.askyesno("Jarvis — подтвердите действие", item[1][:1500], parent=self.root)
                    item[2].set()
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def maybe_hide(self):
        if AUTO_HIDE and jobs.empty() and not state["busy"] and time.time() > state["awaiting_until"]:
            self.root.withdraw()

    def on_enter(self, _):
        t = self.entry.get().strip()
        self.entry.delete(0, "end")
        if t:
            submit(t)

    def on_stop(self):
        cancel.set()
        self.log("⏹ Остановка…")

    def quit(self):
        os._exit(0)


app = App()


# ───────────────────────── голос ─────────────────────────
def speak(text: str):
    clean = re.sub(r"[`*_#>]", "", text)[:600].strip()
    if not clean:
        return
    speaking.set()
    try:
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v.mp3"
            asyncio.run(edge_tts.Communicate(clean, VOICE_NAME).save(str(p)))
            pygame.mixer.music.load(str(p))
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                time.sleep(0.1)
            pygame.mixer.music.unload()
    except Exception as e:
        app.log(f"(озвучка недоступна: {e})")
    finally:
        speaking.clear()


def on_phrase(text: str):
    """Вызывается из потока микрофона для каждой распознанной фразы."""
    app.log(f"🎤 {text}")
    now = time.time()
    m = WAKE_RE.search(text)
    if m:
        cmd = text[m.end():].strip(" ,.!?:-—")
        app.show()
        if not cmd:
            app.set_status("Слушаю, сэр…")
            speak("Слушаю, сэр.")
            state["awaiting_until"] = time.time() + 12
            return
    elif now < state["awaiting_until"]:
        cmd = text.strip()
    else:
        return
    state["awaiting_until"] = 0.0
    submit(cmd)


def start_listener():
    r = sr.Recognizer()
    r.pause_threshold = 0.8
    mic = sr.Microphone()
    with mic as src:
        r.adjust_for_ambient_noise(src, duration=1)

    def cb(rec, audio):
        if speaking.is_set():  # не слушаем самого себя
            return
        try:
            text = rec.recognize_google(audio, language=LISTEN_LANG)
        except sr.UnknownValueError:
            return
        except Exception as e:
            app.log(f"(распознавание: {e})")
            return
        on_phrase(text)

    r.listen_in_background(mic, cb, phrase_time_limit=12)


# ───────────────────────── макросы ─────────────────────────
def load_macros() -> dict:
    try:
        return json.loads(MACROS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _norm(s: str) -> str:
    return re.sub(r"[^\w\s]", " ", s.lower()).strip()


def match_macro(cmd: str) -> str | None:
    """Мини-мозг без моделей: прямое совпадение, затем нечёткое сравнение слов (опечатки, ошибки распознавания)."""
    macros = load_macros()
    low = _norm(cmd)
    for name, m in macros.items():
        if any(p and p in low for p in (_norm(a) for a in [name, *m.get("aliases", [])])):
            return name
    words = low.split()
    best, best_score = None, 0.0
    for name, m in macros.items():
        for ph in [name, *m.get("aliases", []), *m.get("examples", [])]:
            p = _norm(ph)
            if not p:
                continue
            pw = p.split()
            hits = sum(1 for w in pw if difflib.get_close_matches(w, words, n=1, cutoff=0.8))
            score = max(difflib.SequenceMatcher(None, low, p).ratio(), hits / len(pw))
            if score > best_score:
                best, best_score = name, score
    return best if best_score >= 0.8 else None


SITES = {"ютуб": "youtube.com", "youtube": "youtube.com", "гугл": "google.com", "google": "google.com",
         "гитхаб": "github.com", "github": "github.com", "вк": "vk.com", "вконтакте": "vk.com",
         "телеграм": "web.telegram.org", "дискорд": "discord.com/app", "твич": "twitch.tv"}


def local_command(cmd: str) -> str | None:
    """Простые команды без ИИ."""
    low = _norm(cmd)
    if re.search(r"что ты умеешь|помощь|список команд", low):
        names = ", ".join(load_macros()) or "макросов нет"
        return f"Сэр, я знаю макросы: {names}. А ещё время, дату, «открой ютуб», «открой блокнот» и скриншот."
    if re.search(r"который час|сколько времени|скажи время", low):
        return "Сэр, сейчас " + datetime.datetime.now().strftime("%H:%M") + "."
    if re.search(r"какое число|какая дата|какой сегодня день", low):
        return "Сэр, сегодня " + datetime.date.today().strftime("%d.%m.%Y") + "."
    if "скриншот" in low:
        folder = Path.home() / "Pictures"
        folder.mkdir(exist_ok=True)
        path = folder / f"jarvis_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
        pyautogui.screenshot().save(path)
        return f"Скриншот сохранён, сэр: {path.name}."
    m = re.match(r"(открой|запусти|включи)\s+(.+)", low)
    if m:
        target = m.group(2).strip()
        if target in SITES:
            webbrowser.open("https://" + SITES[target])
            return f"Открываю {target}, сэр."
        if os.name == "nt" and re.fullmatch(r"[\w.\- ]{1,40}", target):
            subprocess.Popen(f'start "" "{target}"', shell=True)
            return f"Пробую открыть {target}, сэр."
    return None


def run_macro(name: str) -> str:
    m = load_macros()[name]
    failed = 0
    for c in m.get("commands", []):
        try:
            p = subprocess.run(c, shell=True, capture_output=True, timeout=30)
            failed += p.returncode != 0
        except Exception:
            failed += 1
    if failed:
        return f"Сэр, {name}: не выполнилось команд — {failed}."
    return f"Хорошо, сэр, {m.get('say', name + ' выполнен')}."


# ───────────────────────── инструменты ─────────────────────────
def decode(b: bytes) -> str:
    for enc in ("utf-8", "cp866" if os.name == "nt" else "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode(errors="replace")


def take_screenshot() -> bytes:
    img = pyautogui.screenshot()
    state["scale"] = 1.0
    if img.width > SHOT_W:
        state["scale"] = img.width / SHOT_W
        img = img.resize((SHOT_W, int(img.height / state["scale"])))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def paste_hotkey():
    pyautogui.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")


def run_tool(name: str, a: dict):
    try:
        if name == "fetch_url":
            r = httpx.get(a["url"], follow_redirects=True, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            t = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", r.text)
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t)).strip()[:20_000]
        if name == "run_command":
            if not app.confirm(f"Выполнить команду?\n\n{a['command']}"):
                return "Владелец отклонил команду."
            p = subprocess.run(a["command"], shell=True, capture_output=True, timeout=120, cwd=str(Path.home()))
            return f"exit={p.returncode}\n{decode(p.stdout + p.stderr)[-6000:]}"
        if name == "open_program":
            if not app.confirm(f"Открыть: {a['target']}"):
                return "Владелец отклонил."
            if os.name == "nt":
                os.startfile(a["target"])  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", a["target"]])
            return "Запущено."
        if name == "screenshot":
            png = take_screenshot()
            return [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(png).decode()}}]
        if name == "mouse_click":
            x, y = int(a["x"] * state["scale"]), int(a["y"] * state["scale"])
            (pyautogui.doubleClick if a.get("double") else pyautogui.click)(x, y)
            return "Клик выполнен."
        if name == "type_text":
            t = a["text"]
            if t.isascii():
                pyautogui.write(t, interval=0.02)
            else:  # кириллицу pyautogui не печатает — вставляем через буфер обмена
                pyperclip.copy(t)
                paste_hotkey()
            return "Текст введён."
        if name == "press_key":
            pyautogui.hotkey(*a["keys"].lower().split("+"))
            return "Нажато."
        if name == "read_file":
            return Path(a["path"]).expanduser().read_text(errors="replace")[:60_000]
        if name == "write_file":
            if not app.confirm(f"Записать файл:\n{a['path']}\n({len(a['content'])} символов)"):
                return "Владелец отклонил."
            Path(a["path"]).expanduser().write_text(a["content"], encoding="utf-8")
            return "Файл записан."
    except Exception as e:
        return f"Ошибка инструмента: {e}"
    return f"Неизвестный инструмент: {name}"


def tool_defs():
    def obj(**p):
        return {"type": "object", "properties": p, "required": list(p)}
    return [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
        {"name": "fetch_url", "description": "Скачать страницу по ссылке и вернуть текст.", "input_schema": obj(url={"type": "string"})},
        {"name": "run_command", "description": "Выполнить команду в shell и вернуть вывод (владелец подтверждает).",
         "input_schema": obj(command={"type": "string"})},
        {"name": "open_program", "description": "Запустить программу или открыть файл/URL (владелец подтверждает).",
         "input_schema": obj(target={"type": "string"})},
        {"name": "screenshot", "description": "Скриншот экрана. Координаты mouse_click — в пикселях этого скриншота.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "mouse_click", "description": "Клик мышью по координатам скриншота.",
         "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"},
                                                           "double": {"type": "boolean"}}, "required": ["x", "y"]}},
        {"name": "type_text", "description": "Напечатать текст в активном окне.", "input_schema": obj(text={"type": "string"})},
        {"name": "press_key", "description": "Нажать клавишу или сочетание, например 'ctrl+s'.",
         "input_schema": obj(keys={"type": "string"})},
        {"name": "read_file", "description": "Прочитать текстовый файл.", "input_schema": obj(path={"type": "string"})},
        {"name": "write_file", "description": "Записать текстовый файл (владелец подтверждает).",
         "input_schema": obj(path={"type": "string"}, content={"type": "string"})},
    ]


# ───────────────────────── агент ─────────────────────────
def run_agent(text: str) -> str:
    msgs = history + [{"role": "user", "content": text}]
    tools, final = tool_defs(), ""
    for _ in range(MAX_STEPS):
        if cancel.is_set():
            final = "Остановился."
            break
        resp = claude.messages.create(model=MODEL, max_tokens=4096, system=SYSTEM, tools=tools, messages=msgs)
        msgs.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        if resp.stop_reason == "tool_use":
            results = []
            for b in resp.content:
                if b.type == "tool_use":
                    app.set_status(f"Выполняю: {b.name}")
                    app.log(f"→ {b.name}")
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": run_tool(b.name, b.input)})
            msgs.append({"role": "user", "content": results})
            continue
        if resp.stop_reason == "pause_turn":
            continue
        final = "".join(b.text for b in resp.content if b.type == "text").strip()
        break
    else:
        final = "Сэр, я упёрся в лимит шагов. Скажи, продолжать ли."
    history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": final or "(готово)"}])
    del history[:-HISTORY_LIMIT]
    return final or "Готово."


def submit(cmd: str):
    if STOP_RE.match(cmd):
        cancel.set()
        app.log("⏹ Остановка…")
        return
    jobs.put(cmd)


def worker():
    while True:
        cmd = jobs.get()
        cancel.clear()
        state["busy"] = True
        app.show()
        app.set_status("Думаю…")
        app.log(f"Ты: {cmd}")
        try:
            macro = match_macro(cmd)
            if macro:
                reply = run_macro(macro)
            elif HAS_KEY:
                reply = local_command(cmd) or run_agent(cmd)
            else:
                reply = local_command(cmd) or ("Сэр, без ИИ я знаю только макросы и простые команды. "
                                               "Скажи «что ты умеешь» или добавь ключ.")
        except Exception as e:
            reply = f"Сэр, ошибка: {e}"
        app.log(f"Jarvis: {reply}")
        speak(reply)
        state["busy"] = False
        app.set_status("Слушаю… скажи «Джарвис»")
        app.ui.put(("hide_later",))


def main():
    threading.Thread(target=worker, daemon=True).start()
    try:
        start_listener()
        app.set_status("Слушаю… скажи «Джарвис»")
    except Exception as e:  # нет микрофона / PyAudio — работаем только с текстовым вводом
        app.set_status("Микрофон недоступен — пиши в поле ниже")
        app.log(f"Микрофон не запустился: {e}")
    app.root.mainloop()


if __name__ == "__main__":
    main()
