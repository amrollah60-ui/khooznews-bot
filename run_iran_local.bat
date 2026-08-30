@echo off
REM اجرای ربات روی سرور لوکال (ایران) - منابعی که از گیت‌هاب (آمریکا) بلاک هستند
REM گیت‌هاب مسئول: ایسنا + خوزنیوز + کشتی + بدنسازی
REM این اسکریپت مسئول: MSY + والیبال
chcp 65001 >nul
cd /d "%~dp0"

set TELEGRAM_TOKEN=%%TELEGRAM_TOKEN%%
set TELEGRAM_PROXY=http://127.0.0.1:12334
set ONLY_SOURCES=msy1,msy2,msy3,msy4,volleyball
set STATE_FILE=%~dp0sent_iran.json
set GEMINI_KEY=%%GEMINI_KEY%%

echo khooznews bot - Iran-only sources runner started
echo Press Ctrl+C to stop

:loop
python khooznews_bot.py --once
timeout /t 300 /nobreak >nul
goto loop
