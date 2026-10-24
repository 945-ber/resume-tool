@echo off
chcp 936 >nul
title 简历智造 - 一键启动
echo ==========================================
echo    简历智造 · 一键启动（图形化启动器）
echo ==========================================
echo.
set "PYW=C:\Users\16082\AppData\Local\Programs\Python\Python312\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" "%~dp0启动器.pyw"
echo 启动器窗口已打开：自动拉起服务与公网隧道，并显示访问地址。
echo 若窗口未出现，请直接双击同目录下的 启动器.pyw
timeout /t 4 >nul
exit