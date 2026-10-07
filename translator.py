# =============================================================================
#  translator.py — «EN → RU под курсором» (версия 5.0, БЕЗ библиотеки translators)
# -----------------------------------------------------------------------------
#  Что переделано:
#    • Убрана зависимость `translators` — она регулярно ломается,
#      т.к. движки внутри неё меняются без предупреждения.
#    • Вместо неё — 4 независимых HTTP-эндпоинта, которые мы дёргаем
#      напрямую через `requests`. Если один упал, идём к следующему.
#    • Стало на одну зависимость меньше и на порядок стабильнее.
#
#  ТРЕБОВАНИЯ:
#    1) pip install uiautomation requests
#    2) Запускать от имени администратора — иначе Windows вернёт
#       "Access is denied" при чтении UI чужих процессов.
#
#  Программа реагирует только на текст с латинскими буквами.
#  Русский текст под курсором игнорируется — так и задумано.
# =============================================================================


# -----------------------------------------------------------------------------
#  ИМПОРТЫ
# -----------------------------------------------------------------------------

import os                    # для получения PID нашего процесса
import tkinter as tk         # GUI
import threading             # фоновый перевод
import urllib.parse          # для URL-кодирования текста в запросах
import requests              # HTTP-клиент
import uiautomation as auto  # Windows UI Automation


# -----------------------------------------------------------------------------
#  КОНСТАНТЫ
# -----------------------------------------------------------------------------

# Заголовки для HTTP-запросов. Некоторые сервисы режут клиентов без
# User-Agent, поэтому представляемся обычным Chrome.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

# Кэш: {оригинал: (перевод, имя_движка)}.
# Успешные переводы кэшируются, ошибки — нет.
CACHE: dict[str, tuple[str, str]] = {}

# Интервал опроса курсора (мс).
POLL_INTERVAL_MS = 1000

# Необязательный e-mail для MyMemory (лимит 5000 → 50000 символов/день).
MYMEMORY_EMAIL = ""


# -----------------------------------------------------------------------------
#  ДВИЖКИ ПЕРЕВОДА
#  Каждый — функция, которая принимает текст и возвращает перевод.
#  При неудаче кидает исключение — тогда пробуем следующий движок.
# -----------------------------------------------------------------------------
def _translate_mymemory(text: str) -> str:
    """
    MyMemory — собственный бесплатный API.
    Лимит: 5000 символов/день (или 50000, если указать e-mail).
    Формат ответа: {"responseData": {"translatedText": "..."}}
    """
    params = {"q": text, "langpair": "en|ru"}
    if MYMEMORY_EMAIL:
        params["de"] = MYMEMORY_EMAIL

    r = requests.get(
        "https://api.mymemory.translated.net/get",
        params=params,
        headers=HEADERS,
        timeout=8,
    )
    r.raise_for_status()
    data = r.json()

    translated = data.get("responseData", {}).get("translatedText")

    # Пусто или совпадает с оригиналом — считаем, что не справился.
    if not translated:
        raise RuntimeError(f"пусто ({data.get('responseDetails', '—')})")
    if translated.strip().lower() == text.strip().lower():
        raise RuntimeError("вернул оригинал")

    return translated


def _translate_lingva(text: str) -> str:
    """
    Lingva — публичный прокси Google Translate.
    Ходит в Google со своих серверов, поэтому личных 429 у нас не будет.
    Формат ответа: {"translation": "..."}
    """
    # Текст нужно URL-кодировать: пробелы → %20, кириллица → %D0%...
    url = f"https://lingva.ml/api/v1/en/ru/{urllib.parse.quote(text)}"

    r = requests.get(url, headers=HEADERS, timeout=8)
    r.raise_for_status()
    data = r.json()

    translated = data.get("translation")
    if not translated:
        raise RuntimeError("пусто")

    return translated


def _translate_simplytranslate(text: str) -> str:
    """
    SimplyTranslate — ещё один публичный прокси Google Translate.
    Формат ответа: {"translated-text": "..."}
    """
    r = requests.get(
        "https://simplytranslate.org/api/translate",
        params={"engine": "google", "from": "en", "to": "ru", "text": text},
        headers=HEADERS,
        timeout=8,
    )
    r.raise_for_status()
    data = r.json()

    translated = data.get("translated-text")
    if not translated:
        raise RuntimeError("пусто")

    return translated


def _translate_google(text: str) -> str:
    """
    Google Translate напрямую — резервный вариант.
    Часто отдаёт 429 (Too Many Requests), поэтому идёт последним.
    """
    r = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "en", "tl": "ru", "dt": "t", "q": text},
        headers=HEADERS,
        timeout=8,
    )
    r.raise_for_status()
    data = r.json()

    # Формат: [[["перевод","оригинал",null,...], ...], ...]
    return "".join(part[0] for part in data[0] if part[0])


# Очередь движков. Первый, кто ответит нормально, используется.
ENGINES = [
    ("MyMemory", _translate_mymemory),
    ("Lingva", _translate_lingva),
    ("SimplyTranslate", _translate_simplytranslate),
    ("Google", _translate_google),
]


# -----------------------------------------------------------------------------
#  ГЛАВНАЯ ФУНКЦИЯ ПЕРЕВОДА
# -----------------------------------------------------------------------------
def translate(text: str) -> tuple[str, str]:
    """
    Пробует движки из ENGINES по очереди. Возвращает (перевод, имя_движка).
    Успех кэширует, ошибки — нет (при следующем вызове движок может ожить).
    """
    # 1. Кэш.
    if text in CACHE:
        return CACHE[text]

    errors = []

    # 2. Пробуем движки по очереди.
    for engine_name, engine in ENGINES:
        try:
            result = engine(text)
            # Успех — кэшируем и возвращаем.
            CACHE[text] = (result, engine_name)
            return result, engine_name
        except Exception as e:
            # Короткое описание ошибки без URL (иначе займёт весь экран).
            errors.append(f"{engine_name}: {type(e).__name__}")

    # 3. Ничего не сработало — отдаём сводку.
    msg = "⚠ все движки упали → " + " | ".join(errors)
    return msg, "—"


# -----------------------------------------------------------------------------
#  КЛАСС ПРИЛОЖЕНИЯ
# -----------------------------------------------------------------------------
class App:
    """
    Главное окно: строит виджеты, раз в секунду опрашивает курсор,
    запускает фоновый перевод и обновляет UI.
    """

    def __init__(self):
        # -----------------------------------------------------------------
        #  Окно
        # -----------------------------------------------------------------
        self.root = tk.Tk()
        self.root.title("EN → RU")
        self.root.geometry("420x160+60+60")
        self.root.attributes("-topmost", True)   # всегда поверх других
        self.root.configure(bg="#1e1e1e")

        # -----------------------------------------------------------------
        #  Метка с оригиналом (мелкая, серая, сверху)
        # -----------------------------------------------------------------
        self.src = tk.Label(
            self.root,
            text="наведи курсор на английский текст…",
            wraplength=400,     # автоперенос длинных строк
            anchor="w",
            justify="left",
            fg="#888",
            bg="#1e1e1e",
            font=("Segoe UI", 9),
        )
        self.src.pack(fill="x", padx=10, pady=(10, 4))

        # -----------------------------------------------------------------
        #  Метка с переводом (крупная, снизу)
        # -----------------------------------------------------------------
        self.dst = tk.Label(
            self.root,
            text="",
            wraplength=400,
            anchor="w",
            justify="left",
            fg="#ffffff",
            bg="#1e1e1e",
            font=("Segoe UI", 12, "bold"),
        )
        self.dst.pack(fill="x", padx=10, pady=(0, 2))

        # -----------------------------------------------------------------
        #  Метка со служебной информацией (движок / статус)
        # -----------------------------------------------------------------
        self.info = tk.Label(
            self.root,
            text="",
            wraplength=400,
            anchor="w",
            justify="left",
            fg="#666",
            bg="#1e1e1e",
            font=("Segoe UI", 8),
        )
        self.info.pack(fill="x", padx=10, pady=(0, 10))

        # -----------------------------------------------------------------
        #  Состояние
        # -----------------------------------------------------------------

        # PID нашего процесса — чтобы не переводить самих себя.
        self.own_pid = os.getpid()

        # Текущий текст под курсором.
        self.current = ""

        # Планируем первый тик.
        self.root.after(POLL_INTERVAL_MS, self.tick)

    # ---------------------------------------------------------------------
    #  True, если элемент — часть нашего окна
    # ---------------------------------------------------------------------
    def is_own_window(self, ctrl) -> bool:
        try:
            return ctrl.ProcessId == self.own_pid
        except Exception:
            return False

    # ---------------------------------------------------------------------
    #  Один тик опроса (раз в секунду)
    # ---------------------------------------------------------------------
    def tick(self):
        # --- Получаем контрол под курсором ---
        try:
            x, y = auto.GetCursorPos()
            ctrl = auto.ControlFromPoint(x, y)
        except Exception:
            ctrl = None

        # --- Обрабатываем, только если это чужой контрол ---
        if ctrl and not self.is_own_window(ctrl):
            try:
                text = (ctrl.Name or "").strip()
            except Exception:
                text = ""

            # Есть ли в тексте латинские буквы?
            has_latin = any(c.isalpha() and ord(c) < 128 for c in text)

            # Триггерим перевод только на новом тексте с латиницей.
            if text and has_latin and text != self.current:
                self.current = text

                # Показываем оригинал и статус «перевод…».
                self.src.config(text=text)
                self.dst.config(text="перевод…", fg="#888")
                self.info.config(
                    text="движки: " + ", ".join(n for n, _ in ENGINES)
                )

                # Перевод в фоне.
                threading.Thread(
                    target=self._do_translate,
                    args=(text,),
                    daemon=True,
                ).start()

        # Следующий тик.
        self.root.after(POLL_INTERVAL_MS, self.tick)

    # ---------------------------------------------------------------------
    #  Фоновый перевод
    # ---------------------------------------------------------------------
    def _do_translate(self, text: str):
        result, engine = translate(text)

        def update_ui():
            # Если курсор всё ещё на том же элементе — показываем.
            if self.current == text:
                # Красный цвет — если вернулась ошибка.
                color = "#ff8080" if result.startswith("⚠") else "#ffffff"
                self.dst.config(text=result, fg=color)
                self.info.config(text=f"движок: {engine}")

        self.root.after(0, update_ui)

    # ---------------------------------------------------------------------
    #  Запуск
    # ---------------------------------------------------------------------
    def run(self):
        self.root.mainloop()


# -----------------------------------------------------------------------------
#  ТОЧКА ВХОДА
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    App().run()