@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo 🔍 Проверка виртуального окружения...
if not exist ".venv\Scripts\python.exe" (
    echo ⚙️ Создаю изолированную среду .venv...
    python -m venv .venv
    
    echo 📦 Устанавливаю основные зависимости...
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install customtkinter watchdog google-api-python-client google-auth-oauthlib tiktokautouploader
    
    echo 📦 Устанавливаю instagrapi из исходников...
    .venv\Scripts\python.exe -m pip install https://github.com/adw0rd/instagrapi/archive/master.zip
    
    echo 📦 Устанавливаю браузер для TikTok...
    .venv\Scripts\phantomwright_driver.exe install chromium
)

echo 🚀 Запуск AutoZaliver...
.venv\Scripts\python.exe autozaliver.py

echo.
echo ❌ Программа завершила работу.
pause