package app.bytedev.guard;

import com.google.gson.*;
import java.io.IOException;
import java.net.URI;
import java.nio.file.*;
import java.time.Clock;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import static org.junit.jupiter.api.Assertions.*;

class DurableStateTest {
    @TempDir Path directory;

    Protocol.Config config(Path location, String scope) {
        return new Protocol.Config(URI.create("http://127.0.0.1:5051"), "a".repeat(64),
                scope, "online", "enforce", location);
    }
    Path statePath() { return directory.resolve("companion-state.json"); }
    Path markerPath() { return directory.resolve("companion-initialized.json"); }
    DurableState initialize() throws Exception {
        Protocol.Config config = config(directory, "test-scope");
        DurableState state = new DurableState(config);
        state.installSnapshot(new GuardEngineTest.FakeBridge(config).snapshot);
        return state;
    }
    JsonObject read(Path path) throws IOException {
        return JsonParser.parseString(Files.readString(path)).getAsJsonObject();
    }
    void write(Path path, JsonObject value) throws IOException {
        Files.writeString(path, Protocol.JSON.toJson(value));
    }

    @Test void freshDirectoryInitializesMatchingMarkerAndCanRestart() throws Exception {
        initialize();
        JsonObject state = read(statePath());
        JsonObject marker = read(markerPath());
        assertEquals(1, marker.get("schema_version").getAsInt());
        assertEquals(state.get("initialization_id"), marker.get("initialization_id"));
        assertTrue(new DurableState(config(directory, "test-scope")).ready());
    }
    @Test void gameplayModeReadsDoNotWaitForJournalMonitor() throws Exception {
        Protocol.Config config = config(directory, "test-scope");
        DurableState state = new DurableState(config);
        JsonObject snapshot = new GuardEngineTest.FakeBridge(config).snapshot;
        snapshot.addProperty("mode", "monitor");
        state.installSnapshot(snapshot);
        CompletableFuture<String> read = new CompletableFuture<>();
        synchronized (state) {
            new Thread(() -> read.complete(state.mode())).start();
            assertEquals("monitor", read.get(2, TimeUnit.SECONDS));
        }
        assertEquals("monitor", new DurableState(config).mode());
    }

    @Test void missingStateAfterOutageEnrollmentCannotRestartEvenWithHealthyBridge() throws Exception {
        Protocol.Config config = config(directory, "test-scope");
        GuardEngineTest.FakeBridge bridge = new GuardEngineTest.FakeBridge(config);
        DurableState state = new DurableState(config);
        GuardEngine engine = new GuardEngine(config, bridge, state, Clock.systemUTC());
        engine.maintain(true);
        bridge.offline = true;
        engine.maintain(true);
        GuardEngine.Decision attempt = engine.check("Alice", GuardEngineTest.UUID_A, "8.8.8.8",
                new AtomicBoolean(), true);
        assertTrue(attempt.allowed());
        engine.outcome(attempt.attempt().id(), "joined");
        assertTrue(state.pending() > 0);
        assertEquals("accepted_ip", state.fallback("online:" + GuardEngineTest.UUID_A, "8.8.8.8"));
        Files.delete(statePath());
        bridge.offline = false;
        bridge.calls.clear();
        assertThrows(IOException.class, () -> {
            GuardEngine restarted = new GuardEngine(config, bridge, new DurableState(config), Clock.systemUTC());
            restarted.maintain(true);
        });
        assertTrue(bridge.calls.isEmpty(), "A healthy snapshot cannot erase unreplayed enrollment history");
        assertFalse(Files.exists(statePath()));
    }
    @Test void missingMarkerWithExistingStateFailsClosed() throws Exception {
        initialize();
        Files.delete(markerPath());
        assertThrows(IOException.class, () -> new DurableState(config(directory, "test-scope")));
    }
    @Test void interruptedInitializationMarkerWithoutStateFailsClosed() throws Exception {
        initialize();
        Files.delete(statePath());
        assertThrows(IOException.class, () -> new DurableState(config(directory, "test-scope")));
    }
    @Test void mismatchedInitializationIdFailsClosed() throws Exception {
        initialize();
        JsonObject state = read(statePath());
        state.addProperty("initialization_id", UUID.randomUUID().toString());
        write(statePath(), state);
        assertThrows(IOException.class, () -> new DurableState(config(directory, "test-scope")));
    }
    @Test void persistedStateRequiresJournal() throws Exception {
        initialize();
        JsonObject state = read(statePath());
        state.remove("journal");
        write(statePath(), state);
        assertThrows(IOException.class, () -> new DurableState(config(directory, "test-scope")));
    }
    @Test void markerWriteFailureCannotPublishState() throws Exception {
        Protocol.Config config = config(directory, "test-scope");
        DurableState state = new DurableState(config);
        Files.createDirectory(markerPath());
        assertThrows(IOException.class, () -> state.installSnapshot(new GuardEngineTest.FakeBridge(config).snapshot));
        assertFalse(state.ready());
        assertFalse(Files.exists(statePath()));
        assertThrows(IOException.class, () -> new DurableState(config));
    }
    @Test void newScopeDirectoryCanInitializeWithoutErasingOldMarker() throws Exception {
        initialize();
        Files.delete(statePath());
        Path next = Files.createDirectory(directory.resolve("new-scope"));
        Protocol.Config config = config(next, "replacement-scope");
        DurableState state = new DurableState(config);
        state.installSnapshot(new GuardEngineTest.FakeBridge(config).snapshot);
        assertTrue(state.ready());
        assertTrue(Files.exists(markerPath()));
        assertFalse(Files.exists(statePath()));
    }
    @Test void missingOrReplacedMarkerAlsoStopsExistingProcess() throws Exception {
        DurableState state = initialize();
        JsonObject marker = read(markerPath());
        marker.addProperty("initialization_id", UUID.randomUUID().toString());
        write(markerPath(), marker);
        assertThrows(IOException.class, () -> state.fallback("online:" + GuardEngineTest.UUID_A, "8.8.8.8"));
        Files.delete(markerPath());
        assertThrows(IOException.class, () -> state.fallback("online:" + GuardEngineTest.UUID_A, "8.8.8.8"));
    }
}
