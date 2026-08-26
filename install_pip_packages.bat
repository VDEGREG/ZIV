@ECHO ON
cd /d %~dp0

REM Initialise l'environnement QGIS (auto-détecte le bon OSGEO4W_ROOT)
call "py3-env.bat"
IF %ERRORLEVEL% NEQ 0 (
    ECHO [Génération ZIV] py3-env.bat a échoué - abandon install.
    EXIT /B %ERRORLEVEL%
)

REM Installation via le Python QGIS
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
IF %ERRORLEVEL% EQU 0 GOTO :END

REM Fallback : Python système (si python3 du QGIS introuvable)
ECHO [Génération ZIV] Fallback to system Python...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

:END
pause
