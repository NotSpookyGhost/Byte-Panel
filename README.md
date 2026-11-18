![Byte Panel — Modernize Minecraft Self-Hosting](BP.jpg)

[GitHub](https://github.com/NotSpookyGhost/Byte-Panel) ·
[Report an issue](https://github.com/NotSpookyGhost/Byte-Panel/issues) ·
[Build version](version.txt)

## Overview

Byte Panel is a self-hosted web panel for managing a Minecraft Java Edition server
on Docker or Unraid. Each container manages one server, with its files and panel
settings kept in a persistent `/data` volume.

## Features

- **Multiple server engines:** Vanilla, Paper, Purpur, Fabric and Forge, with live
  version lists and automatic Java runtime selection.
- **Server management:** Start/stop controls, RAM allocation, console commands,
  logs, player history and server properties.
- **Plugins and files:** Upload/delete Paper and Purpur plugins, upload data into
  plugin subfolders, and edit/search text configs.
- **Backups:** Create, download, restore and import server ZIPs.
- **User permissions:** Protected setup owner, administrator accounts and custom
  permissions, including separate backup creation and restore access.
- **Email invitations:** SMTP delivery, customizable welcome/password emails,
  generated temporary passwords and required password changes on first login.
- **Customization:** Server name and icon, favicon, background and appearance settings.

## Using and configuring the panel

### Server controls and settings

During setup, choose your engine, Minecraft version and RAM allocation, or import
an existing server ZIP. Available versions depend on the engine.

Use **Overview** for server controls, **Console** for commands, **Logs** and
**Players** to review activity, and **Properties** to edit server settings.
Console commands do not need a leading `/`: enter `help`, `plugins`, or
`say Hello World`. The panel adds the prefix for modern Paper/Purpur while keeping
the native console format for older engines. Commands already starting with `/`
(including special plugin prefixes such as `//`) are left unchanged.
Stop the server before changing files that it uses, then restart when required.
Use **Panel Settings → General** to change the server name. The product name
stays **Byte Panel**. Settings are grouped into General, Runtime, Backups,
Appearance, Email and Operations.

Under **Panel Settings → Runtime**, two separate options are available (off by default):

- **Automatically start server when Byte Panel starts:** starts the configured
  server once when the panel/container starts. Enabling it does not immediately
  start an offline server.
- **Automatically restart server after a crash:** restarts a panel-managed
  Minecraft process after an unexpected non-zero exit, after a 10-second delay.
  Recovery waits for active backups/file operations and the old process/log reader.
  After three restart attempts within five minutes, automatic recovery stops;
  inspect the logs and press **Start** manually to try again.

The **Stop** button and console `stop` commands cancel pending crash recovery.
Clean exits stay stopped. An intentional shutdown backup uses its own stop/backup/
start workflow, not the crash monitor. Purging, importing or restoring a server
turns both automatic options off. If auto-start remains enabled, a later restart
of the panel/container starts Minecraft again even if it was stopped manually.
These options do not restart Docker itself or detect a server that is hung but
still running.

**Appearance** offers the Obsidian theme and independent lime (default), yellow
or electric-blue accents. Existing light/true-black themes and custom images
remain available. Server status colors are always green (online), amber
(starting/stopping/restarting) and red (offline); unknown status is neutral.

Click your username at the bottom of the sidebar to open **My Account**.
On mobile, use the menu button to open navigation.

Overview shows measured CPU/process memory and recent recorded player connections
when you have Players permission. TPS is marked unavailable because the current
backend does not measure it; it is not a live TPS monitor.

### Server icon

Open **Panel Settings → General → Server icon**, choose a PNG, JPEG or WebP (up to
5 MiB and 16 million pixels), then save. Byte Panel creates a 64×64 PNG without
stretching or cropping and uses it on the **sign-in screen**, in welcome emails,
in the sidebar's **server badge**, and in Minecraft's multiplayer server list. The sign-in card
shows your server name alongside its icon. Restart Minecraft and refresh the list
to see the icon in-game. Byte Panel's top-left navigation logo stays unchanged.

The image is stored as `server/server-icon.png` in the data volume, so server
backups and imports carry it with them. Legacy panel-only logos no longer replace
Byte Panel branding. The favicon and background controls stay under Appearance.
Purging a server clears its server icon.

### Public BlueMap at /map

1. Install the [BlueMap plugin/mod compatible with your server](https://bluemap.bluecolored.de/wiki/getting-started/Installation.html).
   On Paper/Purpur, upload the plugin JAR through **Plugins**, then restart the
   Minecraft server to generate `plugins/BlueMap/` configuration files.
2. Review BlueMap's `core.conf` and enable `accept-download` if you agree to its
   resource download. In `webserver.conf`, keep `enabled: true` and `port: 8100`.
   Optional `ip: "127.0.0.1"` restricts the webserver to the container itself.
   Reload BlueMap or restart Minecraft after editing its configuration.
3. Open **Panel Settings → General → BlueMap**, enable the **public map**, set the
   matching internal port (default **8100**) and save. Use the sidebar **Map** link
   or append `/map` to the panel URL:
   - LAN: `http://192.168.0.10:5050/map`
   - Tunnel: `https://mc.bytedev.app/map`

The panel proxies BlueMap inside the same Minecraft container; it does not install
BlueMap, render maps itself or connect to a separate external BlueMap container.
No extra Docker port publication or Cloudflare tunnel hostname is needed when the
existing hostname already forwards all paths to this panel. Any Cloudflare Access
policy or path-specific rule still applies; bypass caching/HTML rewriting for
`/map/*` if you have enabled custom rules. `/map` redirects to `/map/` so relative
assets resolve, following [BlueMap's subdirectory proxy guidance](https://bluemap.bluecolored.de/wiki/webserver/ReverseProxy.html).

The map is **public without a panel account**, including whatever world details,
markers and player locations BlueMap exposes. Panel management remains protected.
Only install trusted BlueMap add-ons/custom scripts because the map shares the
panel's website origin. Integration starts disabled and is disabled again after a
purge, import or restore; enable it deliberately for the resulting server. If
Minecraft/BlueMap is stopped, the map displays an unavailable message. Compressed
tiles and live player/marker updates are proxied through the same URL.

### Plugins, configs and data files

The **Plugins** tab is available on **Paper and Purpur**.

1. Upload plugin JARs (up to **64 MiB**). They go into the root `plugins` folder.
2. Start/restart the server to let plugins create their folders.
3. Open a plugin folder to upload presets, compressed exports, schematics or
   other non-JAR files (up to **64 MiB**). The upload form shows the destination.
4. Open supported text configs (up to **1 MiB**) to edit them. Use **Find in
   config** or Ctrl/Cmd+F to search, then save and follow the plugin's reload instructions.

Root non-JAR uploads are limited to supported text configs up to 1 MiB. Compressed
files are stored unchanged, not unpacked or imported. Stop the server before
replacing files in use, especially databases.

**LuckPerms example:** open `plugins/LuckPerms`, upload `preset.json.gz`, then run
this in the console while the server is running:

```text
lp import preset.json.gz
```

This merges the exported permissions; uploading alone does not import them.
[More about LuckPerms imports](https://luckperms.net/wiki/General-Commands).

**Delete** removes a plugin JAR but keeps its config folders. Overwriting files
requires confirmation. Previous files are saved in `/data/plugin-config-backups`
before config saves, replacements and JAR deletions. Restart after JAR changes.

### Users and permissions

Configure SMTP first, then open **Users** to create an account with an email
address and either administrator or custom permissions.

New users receive a welcome email followed by a separate temporary-password email
after approximately **12 seconds**. Delivery order depends on the mail provider.
The temporary password expires after **24 hours** and must be changed on first login.
**Resend invitation** generates a replacement and invalidates the old password.

Passwords must be **12–256 characters**, with at least one uppercase letter,
number and ASCII punctuation symbol, such as `!`, `@` or `#`. Users change their
password under **My Account** (click your sidebar username).

Only the original setup owner can purge the server. Creating/managing backups,
restoring backups and importing a server ZIP are separate permissions.
Grant file, console, restore and user-management access only to trusted users.

### Email notifications and templates

1. Open **Panel Settings → Email** and enable delivery.
2. Enter your provider's hostname, port, security mode, credentials, sender
   address and public panel URL. Enable automatic new-user emails.
3. Save the settings, then send a test email before inviting users.

For Gmail, use `smtp.gmail.com`, port `587`, **STARTTLS**, your full email address
and a [Google app password](https://support.google.com/accounts/answer/185833).
Leave the password field blank when saving to keep the existing saved password.

Customize the email designs in:

- [templates/welcomeTemplate.json](templates/welcomeTemplate.json)
- [templates/passwordTemplate.json](templates/passwordTemplate.json)

Use `{{servername}}` for the **Server name** (Settings → General), `{{username}}` for the login name,
`{{login_url}}` for the sign-in link and `{{expires}}` for the password expiry.
`{{password}}` is available **only in the second email**.

Welcome emails automatically show the **Server icon** from General settings above
the welcome heading, while keeping the Byte Panel banner. The icon is embedded
in the email, so loading it does not require a connection to your panel. Email
clients may still apply their own image-display policies. For custom welcome
designs, use `{{server_icon}}` as an **Image block URL** (64 × 64 is recommended).
It is not a normal website link or a text placeholder. If the icon is missing,
unreadable or invalid, the image block is omitted. The password email is unchanged.

Update the matching [plain-text fallbacks](app/templates/emails/) when changing
wording, then rebuild and recreate the container to deploy template edits.
These are JSON design files, not an in-panel email editor. If delivery fails,
check the inbox/spam folder and SMTP settings before resending.

### Backups, imports and resets

Use **Backups** to create or restore server ZIPs, subject to your permissions.
Creation runs in the background with progress checks; new ZIPs appear in the list
only after completion and verification. Configure scheduling and retention under
**Panel Settings → Backups**.

Enable **Shut down server during backups** to gracefully stop a running server,
wait for it to exit, create and verify the ZIP, then start Minecraft again. This
applies to manual and scheduled backups; servers already offline stay offline.
The setting is off by default. Players are disconnected during the backup, and
users with backup creation permission can trigger that downtime. With this option
off, stop Minecraft yourself for a consistent snapshot of worlds and plugin data.

If shutdown takes more than 3 minutes, the backup aborts without force-killing the
server. A cleanly stopped server is restarted even if archiving fails. Abnormal
shutdown or an unfinished log reader requires manual inspection; no archive is
created. If restart fails, any completed ZIP is kept and the panel reports the
restart problem. Failed scheduled shutdown backups wait at least one hour before
retrying, avoiding repeated stop/start cycles. Initial setup backups never start
a server. Offline copies reduce the risk of inconsistent chunk files; they do not
repair existing corruption or protect against unrelated storage failures.

Backup creation cannot overlap with panel start/stop/restart, console commands,
settings changes, restore, import or purge. Restores require Minecraft to be fully
offline. Do not use an external watchdog or plugin to restart Minecraft during an
offline backup. Failed backups leave existing archives intact.

Byte Panel backups include a file called **`ServerEngineInfo`** at the root of the
ZIP. It contains the server metadata Byte Panel uses when importing a server:
the saved Minecraft version, server software/source (Vanilla, Paper, Purpur or
Forge), RAM allocation and any recorded Java or Forge launch settings. This lets
the panel automatically identify the server and apply its saved engine settings
so it can run correctly. Keep this file intact and at the root of the ZIP when
moving a server between Byte Panel installations.

When importing a server ZIP from another source without `ServerEngineInfo`,
Byte Panel asks you to select the Minecraft version, server software and RAM
manually, then creates the file for future backups.

Imports/restores replace the active server, preserve the original owner and acting
user, remove other panel accounts and revoke their previous sessions.

**Purge & Reset** deletes the active server, all panel accounts and player history.
It keeps existing backups, branding and SMTP settings, then returns to setup.

Container restarts and updates keep accounts when the same `/data` volume is used.
Minecraft ZIP backups **exclude panel accounts, session keys and SMTP credentials**;
back up the entire `/data` directory to protect the complete installation.

## Installation

You need Docker, Git and enough free RAM for Minecraft **plus** the host, panel and
other services. These commands build the image locally; no Compose file is required.

### 1. Download and build

```sh
git clone https://github.com/NotSpookyGhost/Byte-Panel.git
cd Byte-Panel
docker build --pull -t mc-panel-build:latest .
```

Already have the source? Run the build command from the folder containing
`Dockerfile`. The image includes Java 8, 11, 17, 21 and 25.

### 2. Create the container

For a **new installation**, run:

```sh
docker run -d \
  --name byte-panel \
  --restart unless-stopped \
  -p 5000:5000 \
  -p 25565:25565 \
  -v /mnt/user/appdata/byte-panel:/data \
  mc-panel-build:latest
```

The example uses an Unraid data path. Replace it with a persistent directory on
your host, separate from the source folder. Port `5000` serves the panel;
`25565` is the default Minecraft game port.

### 3. Complete setup

Open `http://SERVER-IP:5000`, create the owner account and configure your server.
Keep the panel restricted to your trusted network until the owner is created.
Use HTTPS through a trusted reverse proxy for remote panel access.

### Updating an existing installation

Back up your persistent data and any customized source/templates, get the updated
source, then rebuild:

```sh
docker build --pull -t mc-panel-build:latest .
```

Recreate the existing container from the new image, preserving its ports,
environment variables and **the same `/data` mapping**. Restarting the old
container alone does not load the new image. Check the build version in the
panel footer; no Minecraft purge or world rebuild is needed.
