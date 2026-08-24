@echo off
rem Запускатель монитора для Планировщика задач Windows.
cd /d "d:\Claude projects\Sercher"
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
"C:\Python314\python.exe" -m pricewatch >> "data\monitor.log" 2>&1
