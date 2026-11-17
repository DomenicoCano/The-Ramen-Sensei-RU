@echo off
rem Сборка TheRamenSensei_RU_Installer.exe (нужен Python 3.10.1+ и пакеты: UnityPy pyinstaller pillow)
rem На Python 3.10.0 PyInstaller падает в модуле dis — используйте Python 3.12 (например: uv venv --python 3.12 venv312)
python -m PyInstaller --onefile --noconsole --name TheRamenSensei_RU_Installer ^
  --add-data "%~dp0data;data" --paths "%~dp0" --collect-all UnityPy ^
  --exclude-module matplotlib --exclude-module scipy --exclude-module pandas --exclude-module IPython --exclude-module pytest ^
  ru_installer.py
echo Готово: dist\TheRamenSensei_RU_Installer.exe
