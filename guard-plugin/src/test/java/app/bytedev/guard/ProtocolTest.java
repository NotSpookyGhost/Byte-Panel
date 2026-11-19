package app.bytedev.guard;

import com.google.gson.*;
import java.io.*;
import java.net.*;
import java.nio.ByteBuffer;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import org.junit.jupiter.api.*;
import org.junit.jupiter.api.io.TempDir;
import static org.junit.jupiter.api.Assertions.*;

class ProtocolTest {
    @TempDir Path temporary;

    Path configFile(String endpoint) throws Exception {
        Files.createDirectories(temporary.resolve("server"));
        Files.createDirectories(temporary.resolve("private"));
        JsonObject body = new JsonObject();
        body.addProperty("protocol_version", 1);
        body.addProperty("scope", "one-server");
        body.addProperty("token", "x".repeat(64));
        body.addProperty("endpoint", endpoint);
        body.addProperty("identity_mode", "online");
        body.addProperty("mode", "enforce");
        Path file = temporary.resolve("private/config.json");
        Files.writeString(file, Protocol.JSON.toJson(body));
        return file;
    }
    @Test void onlyLiteralLoopbackEndpointAllowed() throws Exception {
        for (String endpoint : List.of("http://example.com:5051", "http://localhost:5051",
                "https://127.0.0.1:5051", "http://127.0.0.1:5051/path", "http://x@127.0.0.1:5051",
                "http://127.0.0.1:5051?leak=secret", "http://127.0.0.1:5051#fragment", "http://127.0.0.1:80")) {
            Path file = configFile(endpoint);
            assertThrows(IOException.class, () -> Protocol.Config.load(file, temporary.resolve("server")), endpoint);
        }
        assertEquals(5051, Protocol.Config.load(configFile("http://127.0.0.1:5051"),
                temporary.resolve("server")).endpoint().getPort());
    }
    @Test void privateConfigurationCannotLiveInServerArchiveRoot() throws Exception {
        Path file = configFile("http://127.0.0.1:5051");
        Path inServer = temporary.resolve("server/config.json");
        Files.copy(file, inServer);
        assertThrows(IOException.class, () -> Protocol.Config.load(inServer, temporary.resolve("server")));
    }
    @Test void booleanAndIntegerProtocolTypesAreStrict() {
        JsonObject value = new JsonObject();
        value.addProperty("complete", "true");
        value.addProperty("revision", 1.5);
        assertThrows(IOException.class, () -> Protocol.bool(value, "complete"));
        assertThrows(IOException.class, () -> Protocol.integer(value, "revision"));
    }
    @Test void responseBodyIsBoundedBeforeAllocation() {
        Bridge.LimitedBodySubscriber subscriber = new Bridge.LimitedBodySubscriber(4);
        boolean[] cancelled = {false};
        subscriber.onSubscribe(new Flow.Subscription() {
            public void request(long amount) {}
            public void cancel() { cancelled[0] = true; }
        });
        subscriber.onNext(List.of(ByteBuffer.wrap(new byte[5])));
        assertTrue(cancelled[0]);
        assertThrows(CompletionException.class, () -> subscriber.getBody().toCompletableFuture().join());
    }
    @Test void bodyAtLimitCompletes() {
        Bridge.LimitedBodySubscriber subscriber = new Bridge.LimitedBodySubscriber(4);
        subscriber.onSubscribe(new Flow.Subscription() {
            public void request(long amount) {}
            public void cancel() {}
        });
        subscriber.onNext(List.of(ByteBuffer.wrap(new byte[]{1,2}), ByteBuffer.wrap(new byte[]{3,4})));
        subscriber.onComplete();
        assertArrayEquals(new byte[]{1,2,3,4}, subscriber.getBody().toCompletableFuture().join());
    }
    @Test void unknownOrAmbiguousUuidCannotBecomeOfficialIdentity() {
        assertThrows(IOException.class, () -> Protocol.identity("online", "Alice", "unknown"));
        assertThrows(IOException.class, () -> Protocol.identity("online", "Alice", "1-1-1-1-1"));
        assertThrows(IOException.class, () -> Protocol.ip("attacker.example"));
    }
}
