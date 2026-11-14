GrabLine (portable)
===================

Run grabline.exe from this folder. Nothing is installed, and no administrator
rights are needed, so this build also works from a USB stick.

First launch
------------
Windows does not recognise the publisher, because this build is not code
signed. Review any Windows reputation/signing prompt and verify the downloaded
package against SHA256SUMS.txt from the release. Do not disable OS protection.

Browser extension
-----------------
GrabLine registers its browser connector on first launch, so the GrabLine
Connect extension can hand downloads over. If the extension says it is not
paired, open Settings > Browser Integration and use "Pair browsers".

What this build does not do
---------------------------
A portable copy does not create Start menu or desktop shortcuts, does not
register magnet links or .torrent files through an installer. Startup settings
and browser pairing can still create per-user integration. Keep this folder's
path stable after pairing. Use Grabline-Setup-<version>.exe for installer tasks.

Your data
---------
Settings, the download list and statistics live in:
    %LOCALAPPDATA%\Grabline

Downloads default to %USERPROFILE%\Downloads\Grabline unless changed in Settings.
Deleting this program folder does not remove your data, downloads, keyring
secrets or necessarily browser/startup registration. Remove those integrations
deliberately if retiring the app. This is not a portable data profile.

Documentation: https://github.com/Gr33nOps/GrabLine/blob/main/docs/README.md
