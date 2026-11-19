package app.bytedev.guard;

import com.google.gson.*;
import java.io.*;
import java.net.URI;
import java.nio.file.*;
import java.time.*;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import static org.junit.jupiter.api.Assertions.*;

class GuardEngineTest {
    @TempDir Path temporary;
    static final String UUID_A = "deadbeef-1234-4234-9234-012345678901";
    static final String UUID_B = "deadbeef-1234-4234-9234-012345678902";
    Protocol.Config config;
    FakeBridge bridge;
    DurableState state;
    GuardEngine engine;

    void setup(String identityMode) throws Exception {
        config = new Protocol.Config(URI.create("http://127.0.0.1:5051"), "a".repeat(64),
                "test-scope", identityMode, "enforce", temporary);
        bridge = new FakeBridge(config);
        state = new DurableState(config);
        engine = new GuardEngine(config, bridge, state, Clock.systemUTC());
        engine.maintain(true);
        assertTrue(engine.ready());
    }
    GuardEngine.Decision join(String name, String uuid, String ip) {
        return engine.check(name, uuid, ip, new AtomicBoolean(), true);
    }

    @Test void firstOfficialPlayerCommitsOnlyAfterJoining() throws Exception {
        setup("online");
        GuardEngine.Decision result = join("Alice", UUID_A, "8.8.8.8");
        assertTrue(result.allowed());
        assertEquals("new_identity", state.fallback("online:" + UUID_A, "9.9.9.9"));
        engine.outcome(result.attempt().id(), "joined");
        assertEquals("accepted_ip", state.fallback("online:" + UUID_A, "8.8.8.8"));
        assertEquals("", state.fallback("online:" + UUID_A, "9.9.9.9"));
    }
    @Test void offlineJoinWithoutPasswordCannotEstablishTrust() throws Exception {
        setup("offline");
        GuardEngine.Decision result = join("Alice", UUID_A, "8.8.8.8");
        engine.outcome(result.attempt().id(), "joined");
        assertEquals("new_identity", state.fallback("offline:alice", "8.8.8.8"));
        engine.outcome(result.attempt().id(), "disconnected");
        engine.outcome(result.attempt().id(), "authenticated");
        assertEquals("new_identity", state.fallback("offline:alice", "8.8.8.8"));
    }
    @Test void offlineAuthenticationAndJoinEitherOrderRequired() throws Exception {
        setup("offline");
        GuardEngine.Decision result = join("Alice", UUID_A, "8.8.8.8");
        engine.outcome(result.attempt().id(), "authenticated");
        assertEquals("new_identity", state.fallback("offline:alice", "8.8.8.8"));
        engine.outcome(result.attempt().id(), "joined");
        assertEquals("accepted_ip", state.fallback("offline:alice", "8.8.8.8"));
    }
    @Test void duplicateOfflineCaseCannotStealReservation() throws Exception {
        setup("offline");
        GuardEngine.Decision original = join("Alice", UUID_A, "8.8.8.8");
        assertTrue(original.allowed());
        assertFalse(join("ALICE", UUID_B, "9.9.9.9").allowed());
        engine.outcome(original.attempt().id(), "disconnected");
        assertTrue(join("ALICE", UUID_B, "9.9.9.9").allowed());
    }
    @Test void officialUsernameChangeStillUsesUuid() throws Exception {
        setup("online");
        GuardEngine.Decision original = join("Alice", UUID_A, "8.8.8.8");
        engine.outcome(original.attempt().id(), "joined");
        engine.outcome(original.attempt().id(), "disconnected");
        bridge.offline = true;
        engine.maintain(true);
        assertTrue(join("NewName", UUID_A, "8.8.8.8").allowed());
        assertFalse(join("NewName", UUID_A, "9.9.9.9").allowed());
    }
    @Test void outageAllowsNewButNotUnfamiliarReturningIp() throws Exception {
        setup("online");
        GuardEngine.Decision first = join("Alice", UUID_A, "8.8.8.8");
        engine.outcome(first.attempt().id(), "joined");
        engine.outcome(first.attempt().id(), "disconnected");
        bridge.offline = true; engine.maintain(true);
        assertFalse(join("Alice", UUID_A, "9.9.9.9").allowed());
        assertTrue(join("Bob", UUID_B, "9.9.9.9").allowed());
        assertTrue(engine.ready(), "An unfamiliar IP denial must not disable all connections");
    }
    @Test void offlineOutageStillRequiresAuthProvider() throws Exception {
        setup("offline");
        bridge.offline = true; engine.maintain(true);
        assertFalse(engine.check("Alice", UUID_A, "8.8.8.8", new AtomicBoolean(), false).allowed());
    }
    @Test void replayIsOrderedAndKeepsLocalEnrollmentUntilAcknowledged() throws Exception {
        setup("online");
        bridge.offline = true; engine.maintain(true);
        GuardEngine.Decision result = join("Alice", UUID_A, "8.8.8.8");
        engine.outcome(result.attempt().id(), "joined");
        engine.outcome(result.attempt().id(), "disconnected");
        assertEquals(3, state.pending());
        assertThrows(IOException.class, () -> state.installSnapshot(bridge.snapshot.deepCopy()));
        DurableState reopened = new DurableState(config);
        assertEquals("accepted_ip", reopened.fallback("online:" + UUID_A, "8.8.8.8"));
        assertEquals(3, reopened.pending());
        bridge.roster("online:" + UUID_A, "8.8.8.8");
        bridge.offline = false; bridge.calls.clear(); engine.maintain(true);
        assertEquals(List.of("/v1/prelogin", "/v1/outcome", "/v1/outcome", "/v1/snapshot", "/v1/heartbeat"), bridge.calls);
        assertEquals(0, state.pending());
        assertEquals("accepted_ip", state.fallback("online:" + UUID_A, "8.8.8.8"));
    }
    @Test void httpOrProtocolFailureNeverUsesFallback() throws Exception {
        setup("online");
        bridge.error = true;
        assertFalse(join("Alice", UUID_A, "8.8.8.8").allowed());
        assertFalse(engine.ready());
        assertEquals(0, state.pending());
        bridge.error = false; bridge.offline = true; engine.maintain(true);
        assertFalse(join("Bob", UUID_B, "9.9.9.9").allowed());
    }
    @Test void explicitDenialDoesNotBecomeFallback() throws Exception {
        setup("online");
        bridge.deny = true;
        assertFalse(join("Alice", UUID_A, "8.8.8.8").allowed());
        assertEquals(0, state.pending());
        assertEquals("new_identity", state.fallback("online:" + UUID_A, "8.8.8.8"));
    }
    @Test void wrongScopeAndPolicyDenyWithoutLearning() throws Exception {
        setup("online");
        bridge.wrongScope = true;
        assertFalse(join("Alice", UUID_A, "8.8.8.8").allowed());
        assertEquals("new_identity", state.fallback("online:" + UUID_A, "8.8.8.8"));
    }
    @Test void wrongPolicyDenyWithoutLearning() throws Exception {
        setup("online");
        bridge.wrongRevision = true;
        assertFalse(join("Alice", UUID_A, "8.8.8.8").allowed());
    }
    @Test void monitorAndOneUseApprovalsDoNotPolluteFallbackCache() throws Exception {
        setup("online");
        bridge.trust = false;
        GuardEngine.Decision result = join("Alice", UUID_A, "8.8.8.8");
        assertTrue(result.allowed());
        engine.outcome(result.attempt().id(), "joined");
        assertEquals("", state.fallback("online:" + UUID_A, "8.8.8.8"));
    }
    @Test void cancellationDoesNotAllowOrReserveIdentity() throws Exception {
        setup("online");
        assertFalse(engine.check("Alice", UUID_A, "8.8.8.8", new AtomicBoolean(true), true).allowed());
        assertTrue(join("Alice", UUID_A, "8.8.8.8").allowed());
    }
    @Test void invalidAndUnknownIdentitiesRejected() throws Exception {
        setup("online");
        assertFalse(join("Alice", "unknown", "8.8.8.8").allowed());
        assertFalse(join("../Alice", UUID_A, "8.8.8.8").allowed());
        assertFalse(join("Alice", UUID_A, "example.com").allowed());
    }
    @Test void storageDisappearanceFailsClosedWithoutReenrolling() throws Exception {
        setup("online");
        Files.delete(temporary.resolve("companion-state.json"));
        bridge.offline = true; engine.maintain(true);
        assertFalse(join("Alice", UUID_A, "8.8.8.8").allowed());
    }
    @Test void missingCacheAndBridgeCannotAssumeEveryoneIsNew() throws Exception {
        setup("online");
        Path other = Files.createDirectory(temporary.resolve("other"));
        Protocol.Config missing = new Protocol.Config(config.endpoint(), config.token(), config.scope(),
                config.identityMode(), "enforce", other);
        bridge.offline = true;
        GuardEngine empty = new GuardEngine(missing, bridge, new DurableState(missing), Clock.systemUTC());
        empty.maintain(true);
        assertFalse(empty.check("Alice", UUID_A, "8.8.8.8", new AtomicBoolean(), true).allowed());
    }
    @Test void corruptCacheCannotBeSilentlyReplaced() throws Exception {
        setup("online");
        Files.writeString(temporary.resolve("companion-state.json"), "not-json");
        assertThrows(IOException.class, () -> new DurableState(config));
    }
    @Test void changedScopeAndModeRejectOldCache() throws Exception {
        setup("online");
        Protocol.Config changed = new Protocol.Config(config.endpoint(), config.token(), "new-scope",
                config.identityMode(), "enforce", temporary);
        assertThrows(IOException.class, () -> new DurableState(changed));
        Protocol.Config offline = new Protocol.Config(config.endpoint(), config.token(), config.scope(),
                "offline", "enforce", temporary);
        assertThrows(IOException.class, () -> new DurableState(offline));
    }
    @Test void ipv6NormalizationUsesSameAcceptedIp() throws Exception {
        setup("online");
        bridge.roster("online:" + UUID_A, "2001:4860:4860::8888");
        state.installSnapshot(bridge.snapshot);
        bridge.offline = true; engine.maintain(true);
        assertTrue(join("Alice", UUID_A, "2001:4860:4860:0:0:0:0:8888").allowed());
    }
    @Test void rejectedReplayKeepsJournalAndBlocksFallback() throws Exception {
        setup("online");
        bridge.offline = true; engine.maintain(true);
        GuardEngine.Decision original = join("Alice", UUID_A, "8.8.8.8");
        engine.outcome(original.attempt().id(), "joined");
        bridge.offline = false; bridge.deny = true; engine.maintain(true);
        assertTrue(state.pending() > 0);
        assertFalse(engine.ready());
        bridge.offline = true; engine.maintain(true);
        assertFalse(join("Bob", UUID_B, "9.9.9.9").allowed());
    }
    @Test void idleHeartbeatDoesNotRewriteOrDownloadWholeRoster() throws Exception {
        setup("online");
        bridge.calls.clear();
        engine.maintain(true);
        assertEquals(List.of("/v1/heartbeat"), bridge.calls);
    }
    @Test void revokedReplayPreservesIdentityButDropsAcceptedIps() throws Exception {
        setup("online");
        bridge.offline = true; engine.maintain(true);
        GuardEngine.Decision original = join("Alice", UUID_A, "8.8.8.8");
        engine.outcome(original.attempt().id(), "joined");
        engine.outcome(original.attempt().id(), "disconnected");
        bridge.offline = false; bridge.revoked = true;
        JsonObject placeholder = new JsonObject();
        placeholder.addProperty("identity", "online:" + UUID_A);
        placeholder.add("accepted_ips", new JsonArray());
        bridge.snapshot.getAsJsonArray("identities").add(placeholder);
        engine.maintain(true);
        assertTrue(engine.ready());
        assertEquals(0, state.pending());
        assertEquals("", state.fallback("online:" + UUID_A, "8.8.8.8"));
    }
    @Test void revokedOutcomeRemovesLocalPermissionEvenWhenNextReplayHitsOutage() throws Exception {
        setup("online");
        GuardEngine.Decision original = join("Alice", UUID_A, "8.8.8.8");
        engine.outcome(original.attempt().id(), "joined");
        engine.outcome(original.attempt().id(), "disconnected");
        assertEquals("accepted_ip", state.fallback("online:" + UUID_A, "8.8.8.8"));
        bridge.revokeOutcomeThenOutage = true;
        engine.maintain(true);
        assertEquals("", state.fallback("online:" + UUID_A, "8.8.8.8"));
        assertTrue(state.pending() > 0);
        assertFalse(join("Alice", UUID_A, "8.8.8.8").allowed());
    }
    @Test void authenticationAfterRevokedJoinCannotRelearnTheIp() throws Exception {
        setup("offline");
        GuardEngine.Decision original = join("Alice", UUID_A, "8.8.8.8");
        engine.outcome(original.attempt().id(), "joined");
        bridge.revokeOutcomeThenOutage = true;
        engine.maintain(true);
        engine.outcome(original.attempt().id(), "authenticated");
        assertEquals("", state.fallback("offline:alice", "8.8.8.8"));
    }

    static final class FakeBridge implements Bridge {
        final Protocol.Config config;
        JsonObject snapshot;
        final List<String> calls = new ArrayList<>();
        boolean offline, error, deny, wrongScope, wrongRevision, revoked, revokeOutcomeThenOutage;
        boolean trust = true;
        FakeBridge(Protocol.Config config) {
            this.config = config; snapshot = Protocol.common(config);
            snapshot.addProperty("mode", "enforce");
            snapshot.addProperty("complete", true);
            snapshot.addProperty("policy_revision", 1);
            snapshot.add("identities", new JsonArray());
        }
        void roster(String identity, String ip) {
            JsonObject record = new JsonObject(); record.addProperty("identity", identity);
            JsonArray ips = new JsonArray(); ips.add(ip); record.add("accepted_ips", ips);
            snapshot.getAsJsonArray("identities").add(record);
        }
        public JsonObject call(String route, JsonObject body) throws IOException {
            calls.add(route);
            if (offline) throw new Bridge.TransportFailure();
            if (error) throw new IOException("HTTP 403");
            if (route.equals("/v1/snapshot")) return snapshot.deepCopy();
            JsonObject reply = Protocol.common(config);
            if (wrongScope) reply.addProperty("scope", "another-server");
            reply.addProperty("policy_revision", wrongRevision ? 2 : 1);
            reply.addProperty("mode", "enforce");
            if (route.equals("/v1/prelogin")) {
                reply.addProperty("attempt_id", body.get("attempt_id").getAsString());
                reply.addProperty("action", deny || revoked ? "deny" : "allow");
                if (revoked) {
                    reply.addProperty("revoked", true);
                    reply.addProperty("replay_accepted", true);
                }
                reply.addProperty("message", "Please contact an administrator.");
                reply.addProperty("reason", "test");
                reply.addProperty("trust_on_success", trust);
            } else {
                reply.addProperty("accepted", true);
                if (route.equals("/v1/outcome") && revokeOutcomeThenOutage) {
                    reply.addProperty("revoked", true);
                    offline = true;
                }
            }
            return reply;
        }
    }
}
