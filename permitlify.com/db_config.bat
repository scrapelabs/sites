@echo off
REM ============================================================================
REM  Shared settings for the local-database scripts.  EDIT THIS FILE ONCE.
REM  Every other db_*.bat / *_local_db.bat reads its values from here.
REM ============================================================================

REM  Password for the LOCAL PostgreSQL "postgres" superuser.
REM  IMPORTANT: use LETTERS and NUMBERS ONLY (no  @ : / \ spaces ).
REM  It is placed inside a connection URL, and those characters would break it.
set "PG_SUPERPASS=aNPq4JFO7g9J0l9WyBsOtmcdsoKkDM"

REM  Name of the local database to create and use.
set "LOCAL_DB=permitlify"

REM  Where PostgreSQL 17 gets installed (this is the default location).
set "PGBIN=C:\Program Files\PostgreSQL\17\bin"

REM  PostgreSQL version the installer downloads. Bump this if the download
REM  ever 404s (pick a current 17.x build number from enterprisedb.com).
set "PGVER=17.6-1"
