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
- **Customization:** Panel name, branding and appearance settings.

## Using and configuring the panel

### Server controls and settings

During setup, choose your engine, Minecraft version and RAM allocation, or import
an existing server ZIP. Available versions depend on the engine.

Use **Overview** for server controls, **Console** for commands, **Logs** and
**Players** to review activity, and **Properties** to edit server settings.
Stop the server before changing files that it uses, then restart when required.
Use **Settings** to change the panel name, branding and appearance.

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
password under **My account**.

Only the original setup owner can purge the server. Creating/managing backups,
restoring backups and importing a server ZIP are separate permissions.
Grant file, console, restore and user-management access only to trusted users.

### Email notifications and templates

1. Open **Settings → Email notifications (SMTP)** and enable delivery.
2. Enter your provider's hostname, port, security mode, credentials, sender
   address and public panel URL. Enable automatic new-user emails.
3. Save the settings, then send a test email before inviting users.

For Gmail, use `smtp.gmail.com`, port `587`, **STARTTLS**, your full email address
and a [Google app password](https://support.google.com/accounts/answer/185833).
Leave the password field blank when saving to keep the existing saved password.

Customize the email designs in:

- [templates/welcomeTemplate.json](templates/welcomeTemplate.json)
- [templates/passwordTemplate.json](templates/passwordTemplate.json)

Use `{{servername}}` for the **Panel Name**, `{{username}}` for the login name,
`{{login_url}}` for the sign-in link and `{{expires}}` for the password expiry.
`{{password}}` is available **only in the second email**.

Update the matching [plain-text fallbacks](app/templates/emails/) when changing
wording, then rebuild and recreate the container to deploy template edits.
These are JSON design files, not an in-panel email editor. If delivery fails,
check the inbox/spam folder and SMTP settings before resending.

### Backups, imports and resets

Use **Backups** to create or restore server ZIPs, subject to your permissions.
Creation runs in the background with progress checks; new ZIPs appear in the list
only after completion and verification. Stop Minecraft first for a consistent snapshot of worlds and
plugin databases. Failed backups report the storage/file error without replacing
existing backups. Automatic backup frequency and retention are configured in
**Settings**. Backup creation cannot overlap with a restore, import or purge.

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
