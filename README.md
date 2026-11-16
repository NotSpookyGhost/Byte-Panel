# Byte Panel

### *A Modern Docker Solution For Hosting Minecraft Servers*

[GitHub repository](https://github.com/NotSpookyGhost/Byte-Panel) |
[Report an issue](https://github.com/NotSpookyGhost/Byte-Panel/issues) |
[Current build version](version.txt)

---

## Overview

**Byte Panel** is a self-hosted Minecraft Java Edition server panel for Docker and
Unraid. Each panel container manages one active Minecraft server, with a web
interface for setup, server controls, console output, players, plugins, backups,
user permissions and email invitations.

This repository contains the Docker build files and application source. Runtime
data is stored separately in the container's persistent `/data` volume.

---

## Key Features

- **Minecraft engines:** Vanilla, Paper, Purpur, Fabric and Forge, with live release
  discovery and engine-specific version availability.
- **Java selection:** Bundled OpenJDK 8, 11, 17, 21 and 25 runtimes, selected for the
  server's requirements.
- **Server controls:** Start/stop controls, configurable RAM, console, logs, player
  history and server properties.
- **Plugin management:** Upload and delete plugin JARs on Paper/Purpur; browse,
  search and edit their text configs with recovery copies before changes.
- **Backups and recovery:** ZIP backups, restore and import, with separate
  backup-management and restore permissions. Only the original setup owner can purge.
- **Panel accounts:** Administrator or custom permissions, protected owner
  account, server-scoped users and enforced password requirements.
- **Email invitations:** Configurable SMTP, editable JSON email designs, separate
  welcome and temporary-password emails, and mandatory password changes on first login.
- **Setup progress:** Completion survives the final saved-state transition;
  temporary polling errors are distinguished from genuine installer failures.

---

## Architecture & Integration

The backend uses Python, Flask and Flask-SocketIO, with an HTML/CSS/JavaScript
frontend. Minecraft runs as a separate process inside the same container.

- **Persistent data:** Minecraft files live in `/data/server`. Account and SMTP
  settings use SQLite outside the Minecraft directory; other runtime settings
  use JSON. Keep the same `/data` mount when replacing the container.
- **Process management:** The panel launches the selected Java runtime and the
  engine's JAR or Forge argument files, and handles console output and shutdown.
- **Ports:** Container TCP port `5000` serves the panel; TCP port `25565` is the
  default Minecraft game port.

---

## Build and run

Install Docker on the host. For a new checkout:

```sh
git clone https://github.com/NotSpookyGhost/Byte-Panel.git
cd Byte-Panel
docker build --pull -t mc-panel-build:latest .
```

If you already have the build files, skip cloning and run the build command from
the directory containing `Dockerfile`. The image tag remains `mc-panel-build:latest`
for compatibility with existing installations.

For a **new installation only**, an example Unraid data path is:

```sh
docker run -d \
  --name byte-panel \
  --restart unless-stopped \
  -p 5000:5000 \
  -p 25565:25565 \
  -v /mnt/user/appdata/byte-panel:/data \
  mc-panel-build:latest
```

Replace the host data path and ports as needed. Keep the runtime data directory
separate from the source/build directory. Open `http://SERVER-IP:5000`, create the
owner account and complete server setup. Restrict access while setup is unclaimed:
the first person to complete setup becomes the owner. Use HTTPS through a trusted
reverse proxy for access outside your trusted network.

### Updating an existing installation

Build the updated source with:

```sh
docker build --pull -t mc-panel-build:latest .
```

Then recreate your existing container from that image, preserving its ports,
environment variables and existing `/data` mapping. Restarting the old container
alone does not switch it to the new image. Verify the new build version in the
panel footer. You do not need to purge the Minecraft server or recreate its world.

Before upgrades, back up the entire persistent data directory as well as any
customized build files. In-panel Minecraft ZIP backups deliberately exclude panel
accounts, session keys and SMTP credentials.

## Plugin management (Paper and Purpur)

The **Plugins** tab is available after setting up a Paper or Purpur server. It manages
the active server's `/data/server/plugins` directory and is hidden and blocked for
other server types.

1. Upload a plugin `.jar` (up to 64 MiB). It must contain `plugin.yml` or
   `paper-plugin.yml`. JARs are installed in the root plugins folder.
2. Start or restart the server so plugins can generate their config folders.
3. Stop the server, open a plugin folder, and select a text config to view or edit.
   Save changes, then start the server again to apply them. The panel does not hot-reload plugins.

Config uploads and the editor support UTF-8 `.yml`, `.yaml`, `.json`, `.toml`,
`.properties`, `.conf`, `.cfg`, `.ini`, `.txt`, `.xml`, `.csv`, `.md`, and `.log`
files up to 1 MiB. Config uploads go into the folder currently open in the browser.
Folders have a folder icon beside their names. The editor's **Find in config** bar
searches the open text (including unsaved edits), counts matches, and supports
Next/Previous and Match case. Use Ctrl/Cmd+F while in the editor, Enter in search
or F3 for the next match, and Shift for the previous match. Search wraps around.
Other binary files can be downloaded but cannot be edited in the text editor.

JAR rows have a **Delete** button instead of Download for users with the
plugin-management permission. Stop the server first, then confirm the exact JAR.
Only that file is removed; its config/data folders remain. Restart to unload the
plugin. A changed JAR must be confirmed again before it can be deleted.

Replacing an upload requires confirmation. Before a config save, file replacement or JAR deletion,
the previous file is copied to `/data/plugin-config-backups` on the persistent data
volume. The editor rejects saves if the file has changed since it was opened. These
copies are kept until you remove them from the data volume; they are outside the
server ZIP backups. To restore a deleted JAR, stop the server and copy its backup
(shown after deletion) to the original plugin path, removing the backup prefix
and trailing .bak so it has its original .jar filename. Then start the server.
Config syntax is not validated by the plain-text editor.

Development checks: `python -m unittest discover -s tests -v`,
`node --check app/static/plugins.js`, `node --check app/static/plugin_search.js`,
`node tests/test_plugin_search.js`, and `node tests/test_plugins_ui.js`.

## Panel accounts and SMTP (1.11.04)

The **Users** tab lets the owner create users, assign an Administrator role or
individual permissions, edit/disable accounts, reset their passwords and delete
them. With no permissions selected, an account can see the overview and its own
account page only. **My account** lets every user change their own password.
New passwords must contain 12–256 characters, at least one uppercase letter
(A–Z), one number (0–9), and one ASCII punctuation symbol (for example `!`, `@`,
`#`, `-` or `_`). Spaces do not count as symbols; lowercase letters are optional.
The server enforces these rules for owner setup and every password change, including
first login. Both forms show the same requirements and identify missing categories.
The compact checklist under the new-password field marks each requirement green as
it is met and updates when characters are removed. It remains readable without
JavaScript; the server still enforces every rule.
Generated 24-character temporary passwords also guarantee all required categories.
Existing passwords keep working until changed; this update does not expire accounts,
force existing users to change passwords, or alter SMTP/app-password requirements.
Usernames are case-insensitive.
The owner cannot be deleted, disabled or demoted through the user editor.
Delegated user managers cannot grant or manage permissions beyond their own.

Only the **original setup owner** can purge the server. This is an owner-only
capability, not a checkbox that can be assigned. Administrator accounts have all
assignable permissions but cannot purge, promote themselves to owner, or change
the protected owner through the Users API.

Backup permissions are independent:

- **Create and manage backups** (backups.create): create, download, rename and
  delete archives; it does not permit restoring them.
- **Restore backups** (backups.restore): list and restore existing backups; it
  does not permit creating, downloading, renaming or deleting archives.
- **Import a server ZIP** (server.import): upload and replace the server with a
  ZIP. Importing a stored backup instead requires backups.restore.

Either backup permission shows the Backups tab. The optional backup-before-restore
button requires create/manage permission. These restrictions are enforced by the
API, not only hidden buttons. Restore/import rights can replace executable server
files and should only be assigned to trusted users.

Existing permissions migrate automatically: old backups.manage becomes
backups.create; old server.reset becomes server.import. Accounts that previously
had both also retain restore access through backups.restore. No non-owner keeps
purge access. Changed permission records revoke their old sessions once, without
changing passwords or requiring a first-login password change.

Existing installations automatically migrate their current administrator to the
owner account, retaining the username/password. Plaintext administrator passwords
are removed from the active configuration. Existing browser sessions must sign in
again after upgrading. New passwords are salted and hashed. Permission changes,
password changes, disabling and deletion invalidate existing sessions; permissions
are checked on backend HTTP and WebSocket operations as well as reflected in the UI.

Accounts belong to the **active server**, not the Docker image:

- Restarting/recreating the container with the same `/data` volume keeps accounts.
- **Purge Ecosystem / Reset**, available only to the setup owner, deletes all accounts, including the owner, and signs
  everyone out. Setup then asks for a new owner. Restrict network access while the
  panel is unclaimed: the first person to complete setup becomes owner.
  It also clears the Minecraft player list, login/IP history, ban history, and
  legacy player-history JSON. The server and its log reader must finish stopping
  before those records are cleared. Existing backups, branding and SMTP settings
  are retained.
- Importing or restoring creates a fresh server scope and revokes old sessions.
  It preserves the original owner and the acting user with their existing
  identities, passwords, roles and permissions, and removes other accounts.
  The acting user is signed back in but never promoted to owner; the owner can
  sign in again using their unchanged credentials. A restore requires only the
  dedicated restore permission, not purge or backup-creation access.
- Server ZIPs never carry accounts, session keys or SMTP credentials. Old archives
  might still contain legacy plaintext credentials; protect or retire those archives.

Under **Settings → Email notifications (SMTP)**, enable delivery and enter your
provider's hostname, port, TLS mode, username/app password, sender address and the
public panel URL. Save, then use **Send test email**. STARTTLS usually uses port 587;
implicit SSL/TLS usually uses 465. An unencrypted trusted relay is supported only
without authentication. Leave the password blank to retain it, or select the
explicit remove-password option to clear it.

For Gmail, use `smtp.gmail.com`, port `587`, STARTTLS and the full email address
as the username. This panel does not implement Google OAuth; use a Google app
password with 2-Step Verification enabled where the account permits it.
See [Google's SMTP settings](https://developers.google.com/workspace/gmail/imap/imap-smtp)
and [app-password help](https://support.google.com/accounts/answer/185833).

SMTP tests report authentication, TLS/certificate, DNS, timeout, and sender or
recipient rejection separately, without exposing credentials or raw provider
responses. A completed test returns HTTP 200 with `success: true` or
`success: false`; failures also include an `error` and `error_code`. This keeps
reverse proxies from replacing a completed SMTP failure result with a 502 HTML
error page. Invalid input and authorization failures still use HTTP error codes.
If the panel/proxy itself returns HTML, the UI reports its HTTP status instead
of a JSON parse error. No tests are retried automatically; check the inbox before
retrying a timeout because delivery may already have occurred.

The SMTP test button is available only after delivery is enabled and the settings
are saved. Changes to any SMTP field disable testing until saved; a visible status
explains whether delivery is disabled, settings are unsaved, or a test is running.
Account scripts/styles use build-version URLs to avoid stale browser/proxy copies
after an update. SMTP failure results include both error and message fields, so
older cached clients display an explanation instead of an empty status box.

In 1.10.07, SMTP network operations allow 30 seconds instead of 10. Errors show
the connection/EHLO/STARTTLS/authentication/submission stage and elapsed time;
the test API also returns `stage` and `elapsed_seconds`. Read timeouts wrapped
by Python as disconnects are still identified as timeouts. Authentication does
not try another mechanism after a credential, policy or temporary-service
rejection; fallback is limited to unsupported AUTH command/mechanism replies.
Cleanup failures cannot turn an already-accepted message into a failed test.
TLS certificate verification remains enabled, and credentials/provider replies
are never included in diagnostics. Recognizable 16-letter Gmail app passwords
are sent without copied whitespace (including passwords saved before this
update); other SMTP hosts and password formats are left unchanged. Outgoing
messages include Date and Message-ID headers.

New accounts require an email address, enabled SMTP delivery, and enabled automatic
account invitations. The panel generates a cryptographically random 24-character
temporary password and stores only its salted hash. The welcome email contains the
username and sign-in link, but no password. After that message is accepted by SMTP,
the panel waits 12 seconds, then sends the password in a separate email. Inbox
arrival times and ordering depend on the mail provider. Temporary passwords expire
after 24 hours. On first login the
user is restricted to My account and must choose a different password before any
panel pages, APIs, or console WebSockets can be used. Chosen passwords are never
emailed. Existing accounts and the owner are not forced to change passwords by
this update; the account database is migrated automatically.

Invitation subjects and welcome messages use the current **Settings > Panel Name**.
In email templates, `{{servername}}` means this panel name, not the
Minecraft server name or SMTP From display name. Renames apply to the next new or
resent invitation without restarting. A missing/blank name falls back to Byte Panel.

New-user and resent invitations use your EmailBuilder JSON designs:

- `templates/welcomeTemplate.json` for the welcome email.
- `templates/passwordTemplate.json` for the temporary-password email.

The panel renders these locally into HTML, preserving block order, colors,
padding, headings, artwork and the button. No external rendering service or Node
runtime is needed. Both emails are multipart/alternative: HTML for normal mail
clients, with the existing `app/templates/emails/welcome.txt` and
`app/templates/emails/temporary_password.txt` as plain-text fallbacks. If changing
the message wording, update the matching fallback too.

Supported blocks are EmailLayout, Text (without Markdown), Heading, Image, Button
and Container, covering both supplied designs. Other block types, raw HTML,
Markdown, invalid styles, broken block references and unsafe links are rejected;
both designs must render successfully before either email is sent. These are
design files, not an in-panel template editor. JSON is read as UTF-8, with or
without a BOM, on every invitation.

Supported tokens are
`{{servername}}`, `{{panel_name}}` (an alias), `{{username}}`, `{{login_url}}`,
`{{password}}`, `{{temporary_password}}` (an alias), and `{{expires}}` (UTC).
Both password tokens contain the generated temporary password in the second email
only, never a user's chosen password or the SMTP password. The welcome template
always receives empty values for both tokens, even if they are added to it.
Spaces inside the braces are
accepted. Replacement happens once, without evaluating expressions; unknown tokens
are left unchanged. Keep the sign-in details, expiry and password-change instructions
when editing templates, then rebuild/recreate the container to deploy template edits.
HTML text values are escaped, so names and password symbols cannot become markup.
Links can use `{{login_url}}` as their entire URL; other substitutions are
rejected in links/image URLs so passwords cannot leak through remote requests.
The welcome artwork link and Server Panel button now use the saved panel URL.
Image URLs remain externally hosted and may require recipients to enable images.
Docker copies the root `templates/` directory to `/templates/` in the image;
editing the build files alone does not update a running container.
The SMTP test remains a single plain-text connection test, not an invitation preview.

Keep the Users page open while the two-email request completes (about 12 seconds
plus SMTP connection/sending time). Other panel requests continue normally. The
panel reports success only when both messages are accepted by SMTP and distinguishes
a failed/unconfirmed second email from a failed welcome. There are no automatic
retries. Before sending the second email, the panel rechecks the account revision,
email address, enabled state, password expiry and SMTP/notification settings; an
edited, deleted, reset, superseded or expired invitation is cancelled. The temporary
password stays only in request memory during the delay, not in a disk queue. If the
container restarts or the request is interrupted, check both inbox/spam messages
before manually resending; delayed sends are not resumed after a restart. Splitting
emails is a presentation choice, not an additional authentication factor.

The user editor no longer accepts manually chosen passwords. **Resend invitation**
or **Send temporary password** generates and emails a replacement, immediately
invalidating the previous password and existing sessions. A successful password
change clears the first-login restriction and invalidates the temporary password
and other sessions. Correcting a pending user's email expires the old invitation;
save the correction and resend. Owner/self changes and delegated permission limits
remain enforced. Temporary passwords never appear in API responses or panel logs.

If delivery fails or is unconfirmed, the account remains pending. Check the inbox
and SMTP settings before resending; do not create the account again. Disabled SMTP
or notifications prevent creation/reset before any password is changed. No mail is
sent merely by saving SMTP settings, and SMTP settings survive server resets.

Private account/SMTP data is stored in `/data/panel_accounts.db` and the generated
session-signing key in `/data/panel_session.key`, outside Minecraft backups.
Protect the data volume and use HTTPS/a trusted reverse proxy for panel access.
Set a strong `PANEL_SECRET_KEY` if supplying your own signing key. Runtime-setting,
plugin-upload, backup and console permissions are powerful and should only be
granted to trusted administrators; they are not an OS-level sandbox for Minecraft.

## Development checks

Install `app/requirements.txt` first. Node.js is used for the JavaScript tests,
not required by the running panel:

```sh
python -m unittest discover -s tests -v
node --check app/static/accounts.js
node --check app/static/panel_access.js
node tests/test_panel_access.js
node tests/test_accounts_ui.js
node tests/test_invitation_ui.js
node tests/test_password_policy_ui.js
node tests/test_plugins_ui.js
node tests/test_plugin_search.js
node tests/test_setup_progress.js
```

Tests use temporary data, SMTP mocks, and the real Python SMTP protocol client
against scripted in-memory server replies (TLS transport is simulated). They
do not start Minecraft, contact a real mail server, or send external mail.
`PANEL_DATA_DIR` can override `/data` for isolated development instances.

## Current Minecraft release support (1.10.02)

Setup now reads Mojang's live stable release manifest instead of a hard-coded
version list. The latest verified release is **26.3** (2026-11-16). The picker
includes stable releases from 1.8.8 onward and shows each engine only for versions
listed by its official service. Snapshots, pre-releases and release candidates are
excluded. Version metadata is cached for five minutes; use **Refresh versions**
to retry a failed lookup. A failed engine service is temporarily hidden with a
warning instead of advertising unsupported combinations.

- Vanilla downloads and Java requirements come from Mojang's version metadata.
- Paper uses the current Fill v3 API, a project-identifying User-Agent, the newest
  stable build for the exact selected version, and the returned download URL.
  If no stable build exists, installation stops with a clear error.
- Purpur resolves the published build for the selected version.
- Fabric discovers a stable loader for the selected game version and a current
  stable installer; neither is pinned to an outdated version.
- Forge resolves the recommended build, falling back to latest, and retains the
  installer/argument-file launch workflow.

Minecraft 26.3 uses Java 25, already included in the Dockerfile. Installation
checks published runtime requirements and rejects versions that require a newer
JRE than the image provides. Vanilla SHA-1 and Paper SHA-256 download checksums are
verified when supplied by their APIs. A release appearing in the picker is not a
promise that every plugin or mod supports it.

This updates the setup options only: it does **not** upgrade an existing server,
change its configured Minecraft version, or modify worlds. Rebuild and recreate
the panel container to deploy these build-file changes.

Sources: [Mojang 26.3 release](https://www.minecraft.net/en-us/article/minecraft-java-edition-26-3),
[Paper downloads API](https://docs.papermc.io/misc/downloads-service/),
[Paper Java requirements](https://docs.papermc.io/paper/getting-started/),
[Fabric metadata API](https://meta.fabricmc.net/).

## Setup completion fix (1.11.05)

The original setup owner can now read the final setup result after completion.
Previously, saving the completed server blocked the last progress request with
HTTP 409, incorrectly showing a build failure at 90%. Setup itself stays locked
after completion, and progress remains owner-only. The saved completion flag
also handles a panel restart after a successful build.

Progress polling distinguishes real installer failures from unavailable progress
checks, retries temporary network/proxy errors without restarting the build, and
asks the owner to sign in again if their session expires. Requests are sequential
and time out after 15 seconds. Existing servers and worlds do not need a purge or
rebuild; rebuild the panel image and recreate its container to deploy this fix.

## Project links and feedback

Source code and project updates:
[NotSpookyGhost/Byte-Panel](https://github.com/NotSpookyGhost/Byte-Panel).

Use [GitHub Issues](https://github.com/NotSpookyGhost/Byte-Panel/issues) for bug
reports and feature requests. Include the panel version, Minecraft version,
engine, deployment details and steps to reproduce the problem. Redact passwords,
SMTP credentials, session tokens and private player information from logs and
screenshots before posting.
