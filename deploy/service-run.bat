@echo off
rem 后台跑网站本体（不开浏览器、不自动关窗口）
rem 用来配合「任务计划程序」做开机自启
cd /d "%~dp0.."
if not exist "data" mkdir "data"
python -u server.py 8000 >> "data\server.log" 2>&1
