@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   简历智能改写工具 - 本地启动
echo   访问地址: http://127.0.0.1:8000
echo   关闭本窗口即停止服务
echo ============================================
"C:\Users\16082\AppData\Local\Programs\Python\Python312\python.exe" -m uvicorn main:app --app-dir "%~dp0backend" --host 127.0.0.1 --port 8000
pause
