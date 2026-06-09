@echo off
REM ============================================================================
REM  Permitlify - set machine-wide environment variables (secrets + config)
REM
REM  1) Fill in the values below (between the quotes).
REM  2) RIGHT-CLICK this file -> "Run as administrator".
REM  3) Re-run setup_windows.bat  (or: nssm restart Permitlify)
REM
REM  These are stored at the Machine level so the Windows service inherits them.
REM  Leave optional lines blank/removed if you do not use that feature.
REM ============================================================================

REM --- REQUIRED ---------------------------------------------------------------
setx /M DJANGO_SECRET_KEY        "jS3brQmvwDW3y37ofzG_I_nmIrZmpUnfUWIvRwEzZv2s_UdoVDVYJa8Uc1TN4HgMqahb6Qk2Mxd1H4k5cqBSNQ"        > nul
setx /M SUPABASE_DATABASE_URL    "postgresql://postgres.jvdywjpicuiefgvdqsjf:aNPq4JFO7g9J0l9WyBsOtmcdsoKkDM@aws-1-us-east-1.pooler.supabase.com:6543/postgres?sslmode=require"        > nul
setx /M SITE_ORIGIN              "https://permitdaily.com"  > nul

REM --- Recommended (cron ingest + scrapers) -----------------------------------
setx /M SCRAPER_INGEST_KEY       "zZ20bnOxSxxq5GlLrYtQ2w-836XNR1eoKhw8ON_seYOnRhfKFEOBUv70CKKc3FME"        > nul
setx /M DO_API_KEY               "doo_v1_539b14c1db99a7bd41a940e31ad915275920a48d3f17987f571cc581be9abe70"        > nul

 
REM --- Optional: Google Sign-In -----------------------------------------------
REM setx /M GOOGLE_CLIENT_ID           ""   > nul
REM setx /M GOOGLE_CLIENT_SECRET       ""   > nul

echo.
echo Done. Machine environment variables have been set.
echo IMPORTANT: now run setup_windows.bat again (or: nssm restart Permitlify)
echo so the service picks up the new values.
pause
