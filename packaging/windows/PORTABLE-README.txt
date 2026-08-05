GrabLine (portable)
===================

Run grabline.exe from this folder. Nothing is installed, and no administrator
rights are needed, so this build also works from a USB stick.

First launch
------------
Windows does not recognise the publisher, because this build is not code
signed. SmartScreen shows "Windows protected your PC". Click "More info",
then "Run anyway". You only see this once per version.

Browser extension
-----------------
GrabLine registers its browser connector on first launch, so the GrabLine
Connect extension can hand downloads over. If the extension says it is not
paired, open Settings > Browser Integration and use "Pair browsers".

What this build does not do
---------------------------
A portable copy does not create Start menu or desktop shortcuts, does not
register magnet links or .torrent files, and does not offer to start with
Windows. Use the installer (Grabline-Setup-<version>.exe) if you want those.

Your data
---------
Settings, the download list and statistics live in:
    %LOCALAPPDATA%\Grabline

Downloads go to your Downloads folder unless you change it in Settings.
Deleting this folder removes the program but not your data or downloads.
