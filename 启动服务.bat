@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   二游 MMD 模型导航 —— 带审核的后端
echo   --------------------------------------------
echo   浏览器会自动打开 http://127.0.0.1:8000
echo   关掉这个窗口 = 停止服务
echo.
echo   管理员口令在 data\admin.token（第一次启动自动生成）
echo.
start "" http://127.0.0.1:8000/
python -u server.py 8000
pause
