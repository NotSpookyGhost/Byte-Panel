# Byte Guard

Byte Guard records Minecraft connection attempts, compares returning
players with previously accepted locations, and offers administrator review.
It is an additional risk check, not account ownership verification or a
replacement for Minecraft authentication.

**Release status: experimental.** Unit tests, protocol tests and a successful
Java build do not establish live server compatibility. The Linux Docker runtime
matrix below has not been executed in this development environment. Complete it
on the exact combination you intend to deploy before relying on Enforce.

## Scope and defaults

- **Off** is the default. Offline servers receive an authentication warning, but
  no plugin is installed merely because online-mode is changed.
- **Monitor** records would-block geographic decisions while allowing those
  connections. It does not turn suspicious joins into trusted new locations.
  Authentication, duplicate-session and invalid-state checks still apply.
- **Enforce** rejects suspicious returning connections at the companion plugin's
  pre-login boundary, before world entry, subject to the limitations below.
- The default geographic radius is **250 km** (approximately 155 miles).
- Only direct **Paper/Purpur** connections on versions in the pinned manifest are
  eligible. Proxy forwarding, Forge, Fabric, Folia and Vanilla are unsupported.
  A Cloudflare tunnel for the panel website is separate from Minecraft forwarding.
- Discord linking, DMs, automatic travel confirmation and automatic IP bans are
  not implemented.

The panel observes attempts that reach its Bukkit pre-login hook, not every TCP
connection or failed Microsoft authentication. A missing or failed companion
cannot protect the server; inspect runtime health. Host administrators and
installed plugins can bypass or interfere with this mechanism. It is not a
network firewall or an independent authentication gateway.

## Setup

1. Back up the installation. Stop Minecraft and keep unrelated file operations
   and automatic starts from interrupting setup.
2. Open **Settings > Security** with **settings.manage** permission. Leave Byte Guard
   **Off**, enter your own MaxMind account ID and license key, then select
   **Save and validate**. Confirm that the City database is ready and current.
3. If the server uses **online-mode=false**, inspect the authentication status.
   Explicitly confirm **Install** for a fresh compatible AuthMe setup or
   **Repair** for an existing compatible installation. Installation/repair
   requires a stopped server. Conflicting providers, unsupported artifacts and
   ambiguous existing data require manual review; the panel does not migrate
   accounts or overwrite another authentication plugin.
4. Optionally preview existing player history. Review the candidates and confirm
   the one-time import only if those historical connections are trustworthy.
5. Save **Monitor**, start Minecraft, and confirm companion/provider health.
   Test with disposable accounts and open **Byte Guard** in the sidebar to review
   connections on the Players page.
6. After completing the live validation checklist, stop Minecraft, select
   **Enforce**, save, then start and recheck health.

The panel also validates newly supplied credentials during Save and validate;
the sequence above keeps activation separate and makes failures easier to
diagnose. A failed database update does not replace the working database or
activate the requested policy. Newly entered credentials can remain saved:
inspect the visible error and retry validation, rather than assuming the old key
was restored. A blank license field keeps the saved key. Removing it requires the
explicit removal control with Byte Guard Off.

Settings and authentication changes do not restart Minecraft. Do not use plugin
hot reloads to install, disable or repair Byte Guard/AuthMe.

## Identity and trust

Official-mode players are keyed by authenticated UUID. Offline-mode players are
keyed by normalized username in a separate namespace. Names and IP addresses
alone do not prove ownership. On a new offline server, the first person who
registers a name with AuthMe can claim it: pre-register or otherwise verify known
players before opening an existing community to untrusted connections.

A new identity can establish its original baseline only after a successful join;
offline mode additionally requires a successful AuthMe login bound to that exact
session. Failed passwords, abandoned registration and disconnected attempts do
not enroll an identity or consume a one-use approval. Concurrent attempts for
the same identity are reserved exclusively.

A previously accepted exact IP is recognized. For a returning player on an
unfamiliar IP, a geographic match requires the same country and a distance
comparison that includes both locations' accuracy radii. Normal nearby joins do
not gradually move the original baseline. Unknown, private, reserved, unmapped
or stale locations cannot justify accepting a returning player's unfamiliar IP.
A first successful connection with no usable location can establish an exact-IP
baseline without a geographic reference.

GeoIP is approximate. VPN exit nodes, shared networks, ISP changes and mobile
carriers can cause false positives or conceal a different person in the same
area. Attackers using an already trusted IP may not be flagged. Do not use a
location match as proof that an account is safe.

### Review and historical data

The Players timeline distinguishes attempted admission, actual joining,
authentication and disconnection. **players.view** can view connection data;
**players.security** can review it:

- **Allow once:** permits that identity and IP for 15 minutes. Successful
  completion consumes the approval; it does not grant permanent exact-IP trust.
- **Trust area:** explicitly adds the reviewed usable location as a reference.
  Verify the player through a separate trusted channel first.
- **Keep blocked:** resolves the review without granting access or banning an IP.
- **Ban IP:** a separate moderation action requiring **players.manage** and
  explicit confirmation. Shared addresses may also block innocent players.

History preview selects the earliest suitable recorded IP for each identity and
applies the current database to it. It is an **estimated historical baseline**,
not proof that the account was authenticated at the time. Import is one-time per
server scope; malformed or unidentifiable entries are skipped. Offline imported
identities still need successful AuthMe authentication before becoming an
authenticated exact-IP outage reference.

Event and review audit retention is 90 days, capped at 100,000 records per table.
Compact known-identity and trusted-reference records are retained independently;
identities are not forgotten merely because their timeline entries expire.

## MaxMind credentials and privacy

Each installation supplies its own credentials; none are baked into the image.
GeoLite2 City is downloaded to private panel storage. Lookups are local:
individual player IPs are not sent to MaxMind as lookup requests. The companion
receives policy/location decisions, not your MaxMind account credentials.

The panel checks for database maintenance daily, validates replacement downloads,
and replaces the database atomically. Data older than 30 days is treated as
stale. Settings shows availability, age and update errors. Replacing credentials
invalidates the old credential-validation status until validation succeeds.

The license key is not returned by the settings API. Private credential files
are stored outside /data/server with restrictive permissions and are excluded
from Minecraft ZIP backups. A host administrator or executable plugin with
filesystem access is still privileged; these are not encrypted-at-rest secrets
protected against the container administrator.

IP addresses, timestamps and approximate locations are sensitive personal data.
Restrict player/security permissions, disclose the monitoring to your players,
and protect exported logs and full-volume backups.

## Offline authentication

Byte Guard Monitor and Enforce require the pinned compatible **AuthMe Reloaded 6.0.1**
integration on offline servers. Fresh managed installations disable session and
premium bypass, restrict unauthenticated players, require registration/login and
use BCrypt. PacketEvents is a required pinned dependency for the managed
inventory-before-login protection. Provider availability and runtime API are
checked as well as files on disk.

Repair preserves existing accounts, database configuration and password-hash
compatibility while applying required security controls. It saves a private
configuration recovery copy. It is not a conversion from another provider.
The generic automatic addon updater does not update managed security plugins;
new security releases require an intentionally reviewed manifest update.

Minecraft backups include AuthMe's local account database and configuration.
These can contain password hashes and external database credentials. An
external AuthMe database needs its own backup: a server ZIP does not snapshot a
remote database. Use a **unique Minecraft authentication password**, never a
Microsoft, email or Byte Panel password.

Known sensitive AuthMe command lines are suppressed at the server logging source
and redacted by the panel as defense in depth. Unrelated plugins can still log
secrets; avoid verbose authentication logging and audit your plugin set. Do not
paste real passwords or database credentials into issue reports.

## Restarts, mode changes and recovery

- Ordinary container/server restarts with the same /data retain Byte Guard state.
  Starting Minecraft validates effective properties, including queued overrides,
  rather than trusting only a previous settings-page selection.
- Changing official/offline identity mode requires explicit Byte Guard reconfiguration
  while stopped. UUID-based and username-based identities are never converted or
  automatically merged. Review any existing references in the selected namespace.
- Import, restore and purge create a new Byte Guard scope, clear applicable trust and
  approvals, rotate private bridge state and leave Byte Guard **Off**. Restored AuthMe
  account files remain Minecraft data; revalidate them before enabling Byte Guard.
  Saved panel MaxMind credentials survive these server operations.
- Turning Byte Guard Off does not uninstall AuthMe or erase its accounts. Treat
  disabling offline authentication as a separate security decision.
- During a temporary bridge transport outage, a complete durable companion roster
  can admit genuinely new identities and known accepted exact IPs; unfamiliar IPs
  for returning players are denied. Offline authentication is still required.
  Missing/corrupt state fails closed. Existing authenticated players stay online.
- Authorization errors, redirects, invalid responses, explicit denials and stale
  scope/protocol are not ordinary outages and do not enable permissive fallback.
  AuthMe failure blocks new offline connections and removes unauthenticated ones.

Check **Settings > Security** health first after a failure. Stop the server before
repairing files. Do not delete companion-state.json or private Byte Guard state to
clear an error: its ordered journal may contain successful offline enrollments
not yet replayed to the panel. Resolve connectivity/storage/provider problems and
let replay complete. If you deliberately disable Byte Guard to recover availability,
understand that geographic enforcement is then absent.

The private authenticated bridge binds loopback inside the container (normally
port 5051). **Do not publish that port** or send it through the public panel,
BlueMap route or tunnel. Only the panel and Minecraft ports are needed.
Reserve 5051 for this bridge; choose other ports for Minecraft and BlueMap.

To preserve or recover private Byte Guard state, back up both
/data/connection-security and /data/.connection-guard-initialized.json while
the panel and Minecraft are stopped. The initialization marker detects a lost
database; deleting a damaged database is not a safe reset. Each companion also
has a companion-initialized.json marker alongside its durable state. Restore
matching files together, and never combine snapshots from different server
scopes. Ordinary Minecraft ZIPs intentionally do not contain these files.

## Compatibility manifest, not certification

The current artifact selection in app/guard_auth.py is:

| Minecraft versions, Paper/Purpur | AuthMe 6.0.1 artifact |
| --- | --- |
| 1.20.6; 1.21 through 1.21.5 | Spigot-Legacy |
| 1.21.6 through 1.21.10 | Spigot-1.21 |
| 1.21.11; 26.1, 26.1.1, 26.1.2, 26.2, 26.3 | Paper |

PacketEvents 2.14.0 is pinned with these selections. Downloads are constrained
to approved publisher CDN URLs and verified for expected size, SHA-512 and plugin
descriptor/version. These checks identify selected artifacts; they do not prove
that a plugin is bug-free or compatible with every other plugin.

The Java companion compiles against Spigot 1.20.6 and is built with Java 21.
Neither a passing build nor publisher version metadata counts as a passing
runtime test. Every new engine/build/provider/dependency combination must pass
the following gate before being described as live-tested.

## Isolated Linux Docker validation

Never run this test plan against production data or production accounts. Use
disposable Minecraft accounts/passwords and a separate world. The lab launcher
does not install Docker, build/pull images, download server/plugin artifacts or
use any existing data volume. Subsequent server setup and an explicitly confirmed
AuthMe installation do download their normal artifacts.

On a Linux Docker host, deliberately build the candidate image, then start a lab:

~~~sh
docker build --pull -t mc-panel-guard-qa:local .
python3 scripts/connection_guard_lab.py start --image mc-panel-guard-qa:local
~~~

The build resolves image/Maven dependencies from the network. The separate lab
command requires that image to exist locally. It creates a uniquely labelled
container and empty volume, publishes panel/Minecraft on random **127.0.0.1-only**
ports, and prints their addresses. It never mounts a host data path or Docker
socket, and never publishes the private bridge. Run the browser/client on that
host or use a deliberately configured SSH tunnel for setup only.

Record the image ID, engine/build, Minecraft version, Java version, AuthMe and
PacketEvents hashes, test date, tester and outcome. One passing row covers only
that exact combination. Keep evidence free of real credentials and redact player
IPs when sharing it.

| Release gate | Required observations | Result |
| --- | --- | --- |
| Fresh online mode | Official UUID identity; first successful join enrolls; failed/abandoned attempts do not | Not run |
| Fresh offline mode | Explicit stopped install; unauthenticated players cannot act; registration plus login enrolls only the actual session | Not run |
| Existing AuthMe | Accounts and database settings survive repair; duplicate/conflicting/unsupported providers block activation | Not run |
| Auth races | Wrong password, reconnect, duplicate names and disconnect-before-auth never enroll or consume an approval | Not run |
| GeoIP decisions | Same accepted IP, nearby different IP, distant/cross-country IP and unusable location each match the stated policy | Not run |
| Monitor vs Enforce | Monitor records without geographic denial; Enforce denial happens before any world interaction | Not run |
| Reviews | Allow-once expires/consumes correctly; trust-area is explicit; keep-blocked never bans; shared-IP ban warning remains separate | Not run |
| Outage/recovery | Transport loss, corrupt/missing roster, failed auth provider and replay preserve identity and fail as documented | Not run |
| Lifecycle | Restart, official/offline switch, restore/import/purge, token rotation and queued property changes never reuse wrong-scope trust | Not run |
| File/credential safety | JAR/config protections, failed-install rollback and source-log redaction; ZIP excludes Byte Guard/MaxMind but contains expected AuthMe data | Not run |
| Permissions | Anonymous/public-map access cannot reach Byte Guard APIs; view-only user cannot change policy, review or ban | Not run |
| Load | Bounded pre-login latency/queues; no HTTP on tick thread; no secret logging during retries or malformed requests | Not run |

Use non-secret sentinel passwords for redaction testing. Never intentionally
disable a provider, corrupt state, fill a disk or simulate an outage on a live
server. Preserve the lab's original files before destructive fault injection.

**Source-IP limitation:** localhost, Docker NAT and SSH tunnels do not supply
independent real geographic origins. The loopback lab cannot certify country or
travel decisions. Perform that part on a separate, attended staging host with
explicitly restricted test-client ingress and consenting testers at known real
source networks. Confirm the IP observed by Minecraft; do not treat forged
headers, proxy forwarding or mocked GeoIP results as a live admission test.
Unit fixtures are useful but must be reported separately.

When finished, use the exact name printed by the launcher:

~~~sh
python3 scripts/connection_guard_lab.py status byte-panel-guard-lab-EXACT_ID
python3 scripts/connection_guard_lab.py cleanup byte-panel-guard-lab-EXACT_ID \
  --confirm byte-panel-guard-lab-EXACT_ID
~~~

Cleanup verifies the exact container/volume names and lab labels before deleting
only that disposable pair. It permanently removes the lab world, accounts,
entered credentials and evidence inside the volume. Export sanitized test notes
first. It never cleans production resources or unrelated failed labs.
