@echo off
chcp 65001 >nul
cd /d %~dp0
echo 正在启动企业微信 AI 智能客服助手（模拟版）...
python server.py
pause
