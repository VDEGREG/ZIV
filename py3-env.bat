@ECHO OFF
REM ---------------------------------------------------------------------------
REM Génération ZIV - environnement Python QGIS (auto-detection)
REM Trouve dynamiquement le dossier OSGEO4W_ROOT à partir, dans l'ordre :
REM   1. La variable d'environnement OSGEO4W_ROOT (positionnée par QGIS lui-même)
REM   2. Une recherche dans "C:\Program Files\QGIS *"
REM Si rien n'est trouvé, on quitte avec un code d'erreur explicite.
REM ---------------------------------------------------------------------------

REM (1) Variable déjà définie par l'utilisateur ou par QGIS lui-même
IF DEFINED OSGEO4W_ROOT GOTO :FOUND

REM (2) Auto-detection dans Program Files
FOR /D %%I IN ("C:\Program Files\QGIS *") DO SET "OSGEO4W_ROOT=%%~I"
IF DEFINED OSGEO4W_ROOT GOTO :FOUND

REM (3) Fallback : OSGeo4W standalone install
IF EXIST "C:\OSGeo4W\bin\o4w_env.bat" SET "OSGEO4W_ROOT=C:\OSGeo4W"
IF DEFINED OSGEO4W_ROOT GOTO :FOUND

ECHO [Génération ZIV] OSGEO4W_ROOT introuvable - aucune install QGIS détectée.
EXIT /B 2

:FOUND
ECHO [Génération ZIV] Using OSGEO4W_ROOT=%OSGEO4W_ROOT%
set PATH=%OSGEO4W_ROOT%\bin;%PATH%
set PATH=%PATH%;%OSGEO4W_ROOT%\apps\qgis\bin

call "%OSGEO4W_ROOT%\bin\o4w_env.bat"
IF EXIST "%OSGEO4W_ROOT%\bin\qt5_env.bat" call "%OSGEO4W_ROOT%\bin\qt5_env.bat"
IF EXIST "%OSGEO4W_ROOT%\bin\qt6_env.bat" call "%OSGEO4W_ROOT%\bin\qt6_env.bat"
call "%OSGEO4W_ROOT%\bin\py3_env.bat"
path %OSGEO4W_ROOT%\apps\qgis\bin;%PATH%

cd /d %~dp0
