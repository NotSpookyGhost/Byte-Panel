package app.bytedev.guard;

import com.google.gson.*;
import java.io.*;
import java.time.Clock;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.LongSupplier;

/** Bukkit-independent protocol state machine; all callers are worker threads. */
final class GuardEngine {
    private static final long REPLAY_BUDGET_NANOS = TimeUnit.SECONDS.toNanos(1);

    record Attempt(String id, String identity, JsonObject request, boolean trust, long createdAt,
                   AtomicBoolean joined, AtomicBoolean authenticated, AtomicBoolean disconnected) {}
    record Decision(boolean allowed, String message, Attempt attempt) {
        static Decision deny(String message) { return new Decision(false, message, null); }
    }
    private final Protocol.Config config;
    private final Bridge bridge;
    private final DurableState state;
    private final Clock clock;
    private final LongSupplier nanoTime;
    private final Map<String, Attempt> attempts = new ConcurrentHashMap<>();
    private final Map<String, String> reservations = new ConcurrentHashMap<>();
    private final Set<String> revokedAttempts = ConcurrentHashMap.newKeySet();
    private volatile boolean hardFailure;
    private volatile boolean transportOutage;
    private volatile boolean initialized;
    private volatile boolean rosterRefreshRequired;
    private volatile String health = "initializing";
    private final Object bridgeLock = new Object();

    GuardEngine(Protocol.Config config, Bridge bridge, DurableState state, Clock clock) {
        this(config, bridge, state, clock, System::nanoTime);
    }
    GuardEngine(Protocol.Config config, Bridge bridge, DurableState state, Clock clock, LongSupplier nanoTime) {
        this.config = config; this.bridge = bridge; this.state = state; this.clock = clock;
        this.nanoTime = nanoTime;
    }
    String health() { return health; }
    String mode() { return state.mode(); }
    boolean offlineRequired() { return config.identityMode().equals("offline") && !mode().equals("off"); }
    boolean ready() { return initialized && !hardFailure && state.ready(); }
    long revision() { return state.revision(); }

    void maintain(boolean authReady) {
        synchronized (bridgeLock) {
            try {
                boolean refresh = !initialized || hardFailure || transportOutage || state.pending() > 0 || rosterRefreshRequired;
                replay();
                if (state.pending() > 0) {
                    initialized = false;
                    health = "replaying";
                    return;
                }
                if (refresh) refreshSnapshot();
                try {
                    heartbeat(authReady);
                } catch (Bridge.TransportFailure e) { throw e; }
                catch (IOException e) {
                    // A policy edit revokes the old revision. Fetch a complete authoritative roster;
                    // never interpret the rejected heartbeat as permission to use outage fallback.
                    refreshSnapshot();
                    heartbeat(authReady);
                }
                initialized = true;
                hardFailure = false;
                transportOutage = false;
                health = offlineRequired() && !authReady ? "authentication_unavailable" : "ready";
                expirePending();
            } catch (Bridge.TransportFailure e) {
                transportOutage = true;
                if (state.ready()) initialized = true;
                health = hardFailure ? "protocol_error" : "bridge_offline";
            } catch (IOException | RuntimeException e) {
                hardFailure = true;
                health = "state_or_protocol_error";
            }
        }
    }

    private void refreshSnapshot() throws IOException {
        JsonObject snapshot = bridge.call("/v1/snapshot", null);
        Protocol.response(snapshot, config);
        state.installSnapshot(snapshot);
        rosterRefreshRequired = false;
    }
    private void heartbeat(boolean authReady) throws IOException {
        JsonObject heartbeat = Protocol.common(config);
        heartbeat.addProperty("policy_revision", state.revision());
        heartbeat.addProperty("mode", state.mode());
        heartbeat.addProperty("ready", state.ready() && (!offlineRequired() || authReady));
        heartbeat.addProperty("auth_ready", authReady);
        heartbeat.addProperty("status", offlineRequired() && !authReady ? "authentication_unavailable" : "ready");
        heartbeat.addProperty("plugin_version", "1.1.0");
        heartbeat.addProperty("pending_events", state.pending());
        Protocol.response(bridge.call("/v1/heartbeat", heartbeat), config);
    }
    private void replay() throws IOException {
        JsonObject entry;
        long started = nanoTime.getAsLong();
        int replayed = 0;
        while (replayed++ < 128 && nanoTime.getAsLong() - started < REPLAY_BUDGET_NANOS
                && (entry = state.head()) != null) {
            JsonObject body = entry.getAsJsonObject("body");
            JsonObject reply = bridge.call(Protocol.string(entry, "route"), body);
            Protocol.response(reply, config);
            if (entry.get("route").getAsString().equals("/v1/prelogin")) {
                boolean revokedReplay = body.has("fallback") && body.get("fallback").getAsBoolean()
                        && reply.has("revoked") && reply.get("revoked").getAsBoolean()
                        && reply.has("replay_accepted") && reply.get("replay_accepted").getAsBoolean();
                if ((!revokedReplay && !"allow".equals(Protocol.string(reply, "action")))
                        || !Protocol.string(body, "attempt_id").equals(Protocol.string(reply, "attempt_id")))
                    throw new IOException("Journal prelogin was denied; manual recovery required");
            } else if (!Protocol.bool(reply, "accepted")) throw new IOException("Journal outcome rejected");
            if (reply.has("revoked") && Protocol.bool(reply, "revoked")) {
                String revokedId = Protocol.string(body, "attempt_id");
                if (attempts.containsKey(revokedId)) revokedAttempts.add(revokedId);
                state.revokeAcceptedIp(Protocol.identity(config.identityMode(), Protocol.string(body, "username"),
                        Protocol.string(body, "uuid")), Protocol.ip(Protocol.string(body, "ip")));
            }
            state.acknowledge(entry.get("sequence").getAsLong());
            rosterRefreshRequired = true;
        }
    }

    Decision check(String username, String uuid, String address, AtomicBoolean cancelled, boolean authReady) {
        if (!ready() || (offlineRequired() && !authReady)) return Decision.deny("Connection security is starting or unavailable. Please try again shortly.");
        String id = UUID.randomUUID().toString();
        String identity;
        try { identity = Protocol.identity(config.identityMode(), username, uuid); address = Protocol.ip(address); }
        catch (IOException e) { return Decision.deny("Unable to verify this connection."); }
        if (reservations.putIfAbsent(identity, id) != null) return Decision.deny("This account already has a connection in progress.");
        boolean retained = false;
        try {
            JsonObject request = Protocol.common(config);
            request.addProperty("attempt_id", id); request.addProperty("username", username);
            request.addProperty("uuid", uuid); request.addProperty("ip", address);
            request.addProperty("policy_revision", state.revision());
            request.addProperty("mode", state.mode());
            request.addProperty("occurred_at", clock.millis());
            boolean trust;
            synchronized (bridgeLock) {
                if (!ready() || cancelled.get()) return Decision.deny("Connection check timed out. Please retry.");
                // A pre-existing journal must stay ordered. While offline, later requests use fallback.
                if (!transportOutage && state.pending() > 0) replay();
                if (!transportOutage && state.pending() > 0)
                    return Decision.deny("Connection history is synchronizing. Please retry shortly.");
                if (!transportOutage && rosterRefreshRequired) refreshSnapshot();
                if (transportOutage) {
                    trust = fallback(identity, address, request);
                } else {
                    JsonObject reply;
                    try { reply = bridge.call("/v1/prelogin", request); }
                    catch (Bridge.TransportFailure e) {
                        transportOutage = true; health = "bridge_offline";
                        trust = fallback(identity, address, request);
                        reply = null;
                    }
                    if (reply != null) {
                        Protocol.response(reply, config);
                        if (!id.equals(Protocol.string(reply, "attempt_id"))
                                || state.revision() != Protocol.integer(reply, "policy_revision")
                                || !state.mode().equals(Protocol.string(reply, "mode")))
                            throw new IOException("Response policy mismatch");
                        String action = Protocol.string(reply, "action");
                        if ("deny".equals(action)) return Decision.deny(safeMessage(reply, id));
                        if (!"allow".equals(action)) throw new IOException("Invalid decision");
                        trust = Protocol.bool(reply, "trust_on_success");
                    } else trust = true;
                }
                Attempt attempt = new Attempt(id, identity, request, trust, clock.millis(),
                        new AtomicBoolean(), new AtomicBoolean(), new AtomicBoolean());
                attempts.put(id, attempt);
                retained = true;
                if (cancelled.get()) {
                    outcome(id, "disconnected");
                    return Decision.deny("Connection check timed out. Please retry.");
                }
                return new Decision(true, "", attempt);
            }
        } catch (IOException | RuntimeException e) {
            // Unknown returning IP is an expected fallback rejection, not a state failure.
            if (!(e instanceof UnfamiliarConnection)) {
                hardFailure = true; health = "state_or_protocol_error";
            }
            return Decision.deny("Connection security cannot verify this connection. Contact a server administrator.");
        } finally { if (!retained) reservations.remove(identity, id); }
    }

    private boolean fallback(String identity, String ip, JsonObject request) throws IOException {
        String decision = state.fallback(identity, ip);
        if (decision.isEmpty()) throw new UnfamiliarConnection();
        request.addProperty("fallback", true);
        request.addProperty("fallback_decision", decision);
        state.append("/v1/prelogin", request, null, null);
        return true;
    }
    private static final class UnfamiliarConnection extends IOException {}
    private static String safeMessage(JsonObject reply, String id) {
        try {
            String message = Protocol.string(reply, "message");
            if (!message.isBlank() && message.length() <= 512
                    && message.chars().noneMatch(c -> c < 32 && c != 10)) return message;
        } catch (IOException ignored) {}
        return "This connection needs administrator review. Reference: " + id.substring(0, 8);
    }

    void outcome(String id, String stage) throws IOException {
        synchronized (bridgeLock) {
        Attempt attempt = attempts.get(id);
        if (attempt == null || attempt.disconnected().get()) return;
        if (!Set.of("joined", "authenticated", "disconnected").contains(stage)) throw new IOException("Invalid outcome");
        if (stage.equals("authenticated") && !config.identityMode().equals("offline")) return;
        boolean joined = attempt.joined().get() || stage.equals("joined");
        boolean authenticated = attempt.authenticated().get() || stage.equals("authenticated");
        JsonObject body = attempt.request().deepCopy();
        body.remove("fallback"); body.remove("fallback_decision");
        body.addProperty("stage", stage);
        body.addProperty("occurred_at", clock.millis());
        boolean completed = !stage.equals("disconnected") && joined
                && (!config.identityMode().equals("offline") || authenticated);
        boolean accepted = completed && attempt.trust() && !revokedAttempts.contains(id);
        boolean recordIdentity = completed && !"off".equals(Protocol.string(attempt.request(), "mode"));
        try {
            state.append("/v1/outcome", body, recordIdentity ? attempt.identity() : null,
                    accepted ? Protocol.string(body, "ip") : null);
        } catch (IOException e) {
            hardFailure = true; health = "storage_error"; throw e;
        }
        attempt.joined().set(joined); attempt.authenticated().set(authenticated);
        if (stage.equals("disconnected")) {
            attempt.disconnected().set(true);
            attempts.remove(id);
            reservations.remove(attempt.identity(), id);
            revokedAttempts.remove(id);
        }
        }
    }
    boolean saveSafeIp(String id) throws IOException {
        synchronized (bridgeLock) {
            Attempt attempt = attempts.get(id);
            if (!ready() || mode().equals("off") || attempt == null || !attempt.joined().get()
                    || attempt.disconnected().get() || (offlineRequired() && !attempt.authenticated().get())
                    || Protocol.integer(attempt.request(), "policy_revision") != state.revision())
                throw new IOException("A current authenticated connection is required");
            replay();
            if (state.pending() != 0 || revokedAttempts.contains(id))
                throw new IOException("The authenticated connection has not been confirmed by the panel");
            JsonObject body = Protocol.common(config);
            body.addProperty("attempt_id", id);
            body.addProperty("policy_revision", state.revision());
            JsonObject reply = bridge.call("/v1/safe-ip", body);
            Protocol.response(reply, config);
            if (!id.equals(Protocol.string(reply, "attempt_id"))
                    || Protocol.integer(reply, "policy_revision") != state.revision()
                    || !Protocol.bool(reply, "accepted"))
                throw new IOException("The panel could not confirm the safe IP request");
            return Protocol.bool(reply, "created");
        }
    }

    private void expirePending() throws IOException {
        for (Attempt attempt : attempts.values()) {
            if (!attempt.joined().get() && clock.millis() - attempt.createdAt() > 120000)
                outcome(attempt.id(), "disconnected");
        }
    }
}
