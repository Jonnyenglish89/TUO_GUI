@echo off
REM Build TUO_GUI.exe locally. Requires Python 3.10+ on PATH.
pip install pyinstaller urllib3 certifi
pyinstaller --onefile --windowed --name TUO_GUI ^
    --icon icon.ico --add-data "icon.ico;." --add-data "app_icon.png;." ^
    --hidden-import tu_inventory --collect-all certifi ^
    run_sim_gui.py
echo.
echo Done - the exe is in dist\TUO_GUI.exe
echo Copy it next to tuo.exe to use it.
pause
