@echo off
REM Liga o agente de transportes no Slack. Feche esta janela (ou Ctrl+C) para desligar.
cd /d "%~dp0"
title Agente Transportes (Slack)
python agente_slack.py
echo.
echo O agente parou. Se houve erro, ele aparece acima.
pause
