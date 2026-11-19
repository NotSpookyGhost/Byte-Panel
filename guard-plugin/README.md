# Byte Guard companion

This is the private, bundled Java companion for Byte Guard.
The internal plugin name remains BytePanelGuard.
It is not a standalone public authentication service and never receives MaxMind
credentials or player passwords.

## Build

Use Java 21 and Maven 3.9.9:

    mvn -B -ntp package

The output is target/BytePanelGuard.jar (plugin version 1.0.0). The Maven
build runs the Java state-machine and protocol tests before packaging. Gson is
relocated into the JAR. Bukkit and Log4j APIs are provided by the server and are
not bundled.

The compile API is the pinned Bukkit-compatible Spigot 1.20.6 API. Runtime support
is deliberately restricted by the panel's compatibility manifest to direct
Paper/Purpur servers. Building against this API is not a claim that every later
server version has passed live integration testing. Proxies, Folia, Forge and
Fabric are not supported.

## Private configuration and bridge

Byte Panel starts Java with -Dbytepanel.guard.config=/private/scope/config.json.
Configuration contains protocol_version: 1, endpoint, token, scope,
identity_mode (online or offline) and mode (off, monitor, enforce).
The endpoint must be an explicit http://127.0.0.1:PORT listener with no path,
query, user information or fragment. No new Docker port is published.

The companion uses authenticated GET /v1/snapshot and POST /v1/prelogin,
POST /v1/outcome, POST /v1/heartbeat. HTTP redirects, invalid responses,
authorization failures, scope/protocol mismatches and explicit denials never
activate outage fallback. Only a temporary transport failure may do so.

The complete roster and ordered journal live in companion-state.json beside
the private configuration, never inside plugins/. Writes use private atomic
replacement and fsync. Entries are not evicted; storage limits instead suspend
new connections. A changed server scope must use a new private directory.

The matching companion-initialized.json marker is durably written before the
first roster. Missing either file after initialization fails closed, even if the
panel is healthy. Recover both matching files together from a private recovery
copy; never delete a marker or roster to trigger fresh enrollment. An interrupted
first initialization also requires administrator recovery. Only an explicitly
new server-scope directory may initialize automatically.

Do not delete a journal to repair a temporary outage: it may contain successful
offline enrollments that have not reached the panel yet. Replay must finish
before replacing the complete roster. Revoked historical attempts can be
acknowledged without restoring their permissions; their identities remain known.

## Authentication and failure behavior

Offline trust requires both successful joining and AuthMe's successful
LoginEvent, bound to the exact Player object and checked against the API.
The username-keyed isAuthenticated API alone is not treated as proof of a new
connection's authentication. Concurrent attempts for the same identity are
reserved exclusively. Wrong passwords, abandoned registration and disconnected
attempts cannot establish trust.

The first provider integration is AuthMe Reloaded 6.0.1; the panel validates
artifacts and configuration before startup. The companion also validates the
runtime provider version/API. Authentication-provider failure blocks new offline
connections and removes unauthenticated sessions. Turning Byte Guard off leaves
the companion inert and does not remove AuthMe accounts.

During a bridge outage, a complete durable roster permits genuinely new identities
and accepted exact IPs. Returning players' unfamiliar IPs are refused. Missing or
corrupt state fails closed. Existing authenticated players remain connected.

Minecraft-facing callbacks do not perform HTTP or bulk disk writes on the tick
thread. Pre-login admission waits at most 1.95 seconds, with bounded workers and
queues. Idle maintenance sends small heartbeats instead of rewriting the complete
roster every few seconds. Journal replay is bounded per maintenance pass.

A source Log4j filter suppresses standard command-audit lines containing common
AuthMe password/verification commands and aliases. This is defense in depth, not
a guarantee that unrelated plugins cannot log secrets. Keep AuthMe sensitive
logging disabled, retain panel-side redaction, and use unique server passwords.

## Live release validation

The unit tests and compile check do not replace Linux Docker integration tests.
Before adding a server/provider combination to the tested compatibility manifest,
verify official and offline login, pre-join rejection before world access,
AuthMe registration/login failure, duplicate-name races, session disconnects,
bridge outage/recovery, policy changes, provider failure and restart recovery on
that exact combination. Verify that source logs and backups exclude private Byte Guard
state and credentials.
