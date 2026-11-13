@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist venv (
  echo Первый запуск: ставлю окружение, это займёт пару минут...
  py -3 -m venv venv || (echo Нужен Python 3.10+ с python.org & pause & exit /b 1)
  call venv\Scripts\activate
  pip install -r requirements.txt || (echo Не удалось поставить зависимости & pause & exit /b 1)
) else (
  call venv\Scripts\activate
)
python jarvis_desktop.py
