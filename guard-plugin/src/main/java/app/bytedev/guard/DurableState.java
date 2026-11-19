package app.bytedev.guard;

import com.google.gson.*;
import java.io.*;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.file.*;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.*;

/** Atomic private roster + ordered write-ahead journal. Never evicts identities. */
final class DurableState {
    private final Protocol.Config config;
    private final Path path;
    private final Path marker;
    private final String initializationId;
    private JsonObject state;
    private boolean existed;
    private boolean failed;

    DurableState(Protocol.Config config) throws IOException {
        this.config = config;
        path = config.stateDir().resolve("companion-state.json");
        marker = config.stateDir().resolve("companion-initialized.json");
        Protocol.safeAncestors(path.getParent());
        existed = Files.exists(path, LinkOption.NOFOLLOW_LINKS);
        boolean marked = Files.exists(marker, LinkOption.NOFOLLOW_LINKS);
        if (existed != marked)
            throw new IOException("Incomplete private state; administrator recovery required");
        initializationId = marked ? readMarker() : UUID.randomUUID().toString();
        if (existed) {
            checkFile();
            try {
                state = JsonParser.parseString(Files.readString(path)).getAsJsonObject();
                validate(state, true);
            } catch (RuntimeException e) { throw new IOException("Corrupt companion state; administrator recovery required"); }
        }
    }

    private void validate(JsonObject value, boolean persisted) throws IOException {
        Protocol.response(value, config);
        if (!config.identityMode().equals(Protocol.string(value, "identity_mode")))
            throw new IOException("Identity mode changed; administrator reconfiguration required");
        if (!Protocol.bool(value, "complete")) throw new IOException("Incomplete companion roster");
        if (!Set.of("off", "monitor", "enforce").contains(Protocol.string(value, "mode")))
            throw new IOException("Invalid Guard mode");
        if (Protocol.integer(value, "policy_revision") < 0) throw new IOException("Invalid policy revision");
        if (!value.has("identities") || !value.get("identities").isJsonArray()) throw new IOException("Missing roster");
        Set<String> identities = new HashSet<>();
        for (JsonElement raw : value.getAsJsonArray("identities")) {
            JsonObject record = raw.getAsJsonObject();
            String identity = Protocol.string(record, "identity");
            if (!validIdentity(identity) || !identities.add(identity)) throw new IOException("Invalid or duplicate identity");
            if (!record.has("accepted_ips") || !record.get("accepted_ips").isJsonArray()) throw new IOException("Missing accepted IPs");
            for (JsonElement ip : record.getAsJsonArray("accepted_ips")) Protocol.ip(ip.getAsString());
        }
        if (persisted && (!initializationId.equals(Protocol.string(value, "initialization_id"))
                || !value.has("journal")))
            throw new IOException("Incomplete private state; administrator recovery required");
        if (value.has("journal")) {
            if (!value.get("journal").isJsonArray() || value.getAsJsonArray("journal").size() > 20000)
                throw new IOException("Invalid journal");
            long previous = -1;
            for (JsonElement raw : value.getAsJsonArray("journal")) {
                JsonObject entry = raw.getAsJsonObject();
                long sequence = Protocol.integer(entry, "sequence");
                if (sequence <= previous || !Set.of("/v1/prelogin", "/v1/outcome").contains(Protocol.string(entry, "route")))
                    throw new IOException("Invalid journal ordering");
                JsonObject body = entry.getAsJsonObject("body");
                Protocol.response(body, config);
                Protocol.identity(config.identityMode(), Protocol.string(body, "username"), Protocol.string(body, "uuid"));
                Protocol.ip(Protocol.string(body, "ip"));
                UUID.fromString(Protocol.string(body, "attempt_id"));
                previous = sequence;
            }
            if (Protocol.integer(value, "next_sequence") <= previous) throw new IOException("Invalid journal counter");
        }
    }

    private boolean validIdentity(String identity) {
        if (config.identityMode().equals("offline")) return identity.matches("offline:[a-z0-9_]{1,16}");
        if (!identity.startsWith("online:")) return false;
        try { return UUID.fromString(identity.substring(7)).toString().equals(identity.substring(7)); }
        catch (IllegalArgumentException e) { return false; }
    }
    synchronized boolean ready() { return state != null && !failed; }
    synchronized String mode() { return state == null ? "enforce" : state.get("mode").getAsString(); }
    synchronized long revision() { return state == null ? -1 : state.get("policy_revision").getAsLong(); }
    synchronized String fallback(String identity, String ip) throws IOException {
        ensureReady();
        for (JsonElement raw : state.getAsJsonArray("identities")) {
            JsonObject record = raw.getAsJsonObject();
            if (!record.get("identity").getAsString().equals(identity)) continue;
            for (JsonElement known : record.getAsJsonArray("accepted_ips"))
                if (Protocol.ip(known.getAsString()).equals(ip)) return "accepted_ip";
            return "";
        }
        return "new_identity";
    }
    synchronized int pending() { return state == null ? 0 : state.getAsJsonArray("journal").size(); }
    synchronized JsonObject head() {
        if (pending() == 0) return null;
        return state.getAsJsonArray("journal").get(0).getAsJsonObject().deepCopy();
    }

    synchronized void installSnapshot(JsonObject snapshot) throws IOException {
        try { validate(snapshot, false); }
        catch (RuntimeException e) { throw new IOException("Invalid complete roster"); }
        if (failed) throw new IOException("Private state failed");
        // Do not overwrite locally authenticated enrollments that have not been replayed.
        if (pending() != 0) throw new IOException("Journal must replay before replacing roster");
        JsonObject next = snapshot.deepCopy();
        next.addProperty("initialization_id", initializationId);
        next.add("journal", new JsonArray());
        next.addProperty("next_sequence", state == null ? 1 : state.get("next_sequence").getAsLong());
        persist(next);
    }

    synchronized long append(String route, JsonObject body, String acceptedIdentity, String acceptedIp) throws IOException {
        ensureReady();
        if (pending() >= 20000) throw new IOException("Private journal full; connections are paused until recovery");
        JsonObject next = state.deepCopy();
        long sequence = next.get("next_sequence").getAsLong();
        JsonObject entry = new JsonObject();
        entry.addProperty("sequence", sequence);
        entry.addProperty("route", route);
        entry.add("body", body.deepCopy());
        next.getAsJsonArray("journal").add(entry);
        next.addProperty("next_sequence", sequence + 1);
        if (acceptedIdentity != null) addAccepted(next, acceptedIdentity, acceptedIp);
        persist(next);
        return sequence;
    }

    synchronized void replace(long sequence, JsonObject body) throws IOException {
        ensureReady();
        JsonObject next = state.deepCopy();
        for (JsonElement raw : next.getAsJsonArray("journal")) {
            JsonObject entry = raw.getAsJsonObject();
            if (entry.get("sequence").getAsLong() == sequence) {
                entry.add("body", body.deepCopy()); persist(next); return;
            }
        }
        throw new IOException("Missing journal entry");
    }

    synchronized void acknowledge(long sequence) throws IOException {
        ensureReady();
        JsonObject next = state.deepCopy();
        JsonArray journal = next.getAsJsonArray("journal");
        for (int i = 0; i < journal.size(); i++) {
            if (journal.get(i).getAsJsonObject().get("sequence").getAsLong() == sequence) {
                journal.remove(i); persist(next); return;
            }
        }
    }

    synchronized void revokeAcceptedIp(String identity, String ip) throws IOException {
        ensureReady();
        JsonObject next = state.deepCopy();
        for (JsonElement raw : next.getAsJsonArray("identities")) {
            JsonObject record = raw.getAsJsonObject();
            if (!record.get("identity").getAsString().equals(identity)) continue;
            if (ip == null) return;
            JsonArray ips = record.getAsJsonArray("accepted_ips");
            for (int i = ips.size() - 1; i >= 0; i--) {
                if (Protocol.ip(ips.get(i).getAsString()).equals(ip)) ips.remove(i);
            }
            // Keep identity existence. A revoked IP must not turn a returning player into new.
            persist(next);
            return;
        }
    }

    private void addAccepted(JsonObject value, String identity, String ip) {
        JsonArray roster = value.getAsJsonArray("identities");
        for (JsonElement raw : roster) {
            JsonObject record = raw.getAsJsonObject();
            if (!record.get("identity").getAsString().equals(identity)) continue;
            if (ip == null) return;
            JsonArray ips = record.getAsJsonArray("accepted_ips");
            for (JsonElement known : ips) if (known.getAsString().equals(ip)) return;
            ips.add(ip); return;
        }
        JsonObject record = new JsonObject();
        record.addProperty("identity", identity);
        JsonArray ips = new JsonArray();
        if (ip != null) ips.add(ip);
        record.add("accepted_ips", ips); roster.add(record);
    }

    private void ensureReady() throws IOException {
        if (!ready()) throw new IOException("Complete private roster is unavailable");
        checkFile();
    }
    private void checkFile() throws IOException {
        Protocol.safeAncestors(path.getParent());
        if (!initializationId.equals(readMarker())) throw new IOException("Private initialization marker changed");
        if (Files.isSymbolicLink(path) || !Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)
                || Files.size(path) > Protocol.MAX_BODY) throw new IOException("Unsafe or oversized private state");
    }
    private String readMarker() throws IOException {
        Protocol.safeAncestors(marker.getParent());
        if (Files.isSymbolicLink(marker) || !Files.isRegularFile(marker, LinkOption.NOFOLLOW_LINKS)
                || Files.size(marker) > 4096) throw new IOException("Missing or unsafe private initialization marker");
        try {
            JsonObject value = JsonParser.parseString(Files.readString(marker)).getAsJsonObject();
            Protocol.response(value, config);
            if (Protocol.integer(value, "schema_version") != 1
                    || !config.identityMode().equals(Protocol.string(value, "identity_mode")))
                throw new IOException("Invalid private initialization marker");
            String id = Protocol.string(value, "initialization_id");
            if (!UUID.fromString(id).toString().equals(id)) throw new IOException("Invalid initialization ID");
            return id;
        } catch (RuntimeException e) { throw new IOException("Corrupt private initialization marker"); }
    }
    private void createMarker() throws IOException {
        JsonObject value = Protocol.common(config);
        value.addProperty("schema_version", 1);
        value.addProperty("initialization_id", initializationId);
        byte[] bytes = Protocol.JSON.toJson(value).getBytes(java.nio.charset.StandardCharsets.UTF_8);
        // CREATE_NEW never overwrites an existing marker. A crash during this write
        // leaves an incomplete marker and no state, deliberately requiring recovery.
        try (FileChannel out = FileChannel.open(marker, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)) {
            try { Files.setPosixFilePermissions(marker, PosixFilePermissions.fromString("rw-------")); }
            catch (UnsupportedOperationException ignored) {}
            ByteBuffer buffer = ByteBuffer.wrap(bytes);
            while (buffer.hasRemaining()) out.write(buffer);
            out.force(true);
        }
        syncDirectory();
    }
    private void syncDirectory() throws IOException {
        // Windows cannot open directories. Production Linux persists directory entries.
        if (!System.getProperty("os.name").startsWith("Windows")) {
            try (FileChannel directory = FileChannel.open(path.getParent(), StandardOpenOption.READ)) { directory.force(true); }
        }
    }
    private void persist(JsonObject next) throws IOException {
        Path temp = null;
        try {
            Protocol.safeAncestors(path.getParent());
            if (existed) checkFile();
            else if (Files.exists(path, LinkOption.NOFOLLOW_LINKS)) throw new IOException("Unexpected private state");
            if (state != null && state.equals(next)) return;
            validate(next, true);
            byte[] bytes = Protocol.JSON.toJson(next).getBytes(java.nio.charset.StandardCharsets.UTF_8);
            if (bytes.length > Protocol.MAX_BODY) throw new IOException("Private roster storage limit reached");
            // Publish and fsync the tombstone first: losing state can never make an
            // initialized directory look fresh, including after outage enrollments.
            if (!existed) createMarker();
            temp = Files.createTempFile(path.getParent(), ".guard-state-", ".tmp");
            try {
                Files.setPosixFilePermissions(temp, PosixFilePermissions.fromString("rw-------"));
            } catch (UnsupportedOperationException ignored) {}
            try (FileChannel out = FileChannel.open(temp, StandardOpenOption.WRITE)) {
                ByteBuffer buffer = ByteBuffer.wrap(bytes);
                while (buffer.hasRemaining()) out.write(buffer);
                out.force(true);
            }
            Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            syncDirectory();
            existed = true;
            state = next;
        } catch (IOException | RuntimeException e) {
            failed = true;
            throw new IOException("Could not persist private connection state");
        } finally {
            if (temp != null) Files.deleteIfExists(temp);
        }
    }
}
