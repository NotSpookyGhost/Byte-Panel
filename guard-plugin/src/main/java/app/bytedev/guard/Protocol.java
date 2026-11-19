package app.bytedev.guard;

import com.google.gson.*;
import java.io.IOException;
import java.net.*;
import java.nio.file.*;
import java.util.*;

final class Protocol {
    static final Gson JSON = new GsonBuilder().disableHtmlEscaping().create();
    static final int VERSION = 1;
    static final int MAX_BODY = 64 * 1024 * 1024;

    record Config(URI endpoint, String token, String scope, String identityMode, String mode, Path stateDir) {
        static Config load(Path configFile, Path serverDir) throws IOException {
            Path path = configFile.toAbsolutePath().normalize();
            safeAncestors(path.getParent());
            if (Files.isSymbolicLink(path) || !Files.isRegularFile(path, LinkOption.NOFOLLOW_LINKS)
                    || Files.size(path) > 16384) throw new IOException("Invalid private configuration file");
            Path root = path.getParent().toRealPath();
            Path server = serverDir.toAbsolutePath().normalize().toRealPath();
            if (root.startsWith(server)) throw new IOException("Private configuration must be outside server files");
            try {
                JsonObject value = JsonParser.parseString(Files.readString(path)).getAsJsonObject();
                if (integer(value, "protocol_version") != VERSION) throw new IOException("Unsupported protocol");
                URI endpoint = URI.create(string(value, "endpoint"));
                if (!"http".equals(endpoint.getScheme()) || !"127.0.0.1".equals(endpoint.getHost())
                        || endpoint.getPort() < 1024 || endpoint.getPort() > 65535
                        || endpoint.getRawUserInfo() != null || endpoint.getRawQuery() != null
                        || endpoint.getRawFragment() != null || !Set.of("", "/").contains(endpoint.getPath()))
                    throw new IOException("Bridge must be an explicit loopback HTTP listener");
                String token = string(value, "token");
                String scope = string(value, "scope");
                String mode = string(value, "identity_mode");
                if (token.length() < 32 || token.length() > 512 || token.chars().anyMatch(Character::isISOControl)
                        || scope.isBlank() || scope.length() > 128 || !Set.of("online", "offline").contains(mode))
                    throw new IOException("Invalid private configuration");
                String guardMode = string(value, "mode");
                if (!Set.of("off", "monitor", "enforce").contains(guardMode)) throw new IOException("Invalid Guard mode");
                return new Config(endpoint, token, scope, mode, guardMode, root);
            } catch (RuntimeException e) { throw new IOException("Invalid private configuration"); }
        }
    }

    static void safeAncestors(Path path) throws IOException {
        Path current = path.toAbsolutePath().normalize().getRoot();
        for (Path component : path.toAbsolutePath().normalize()) {
            current = current.resolve(component);
            if (Files.isSymbolicLink(current) || !Files.isDirectory(current, LinkOption.NOFOLLOW_LINKS))
                throw new IOException("Unsafe private state directory");
        }
    }

    static String string(JsonObject value, String name) throws IOException {
        if (!value.has(name) || !value.get(name).isJsonPrimitive()
                || !value.getAsJsonPrimitive(name).isString()) throw new IOException("Missing protocol field: " + name);
        return value.get(name).getAsString();
    }
    static long integer(JsonObject value, String name) throws IOException {
        try {
            if (!value.has(name) || !value.get(name).isJsonPrimitive()
                    || !value.getAsJsonPrimitive(name).isNumber()) throw new IOException("Missing protocol number");
            return Long.parseLong(value.get(name).getAsString());
        } catch (NumberFormatException e) { throw new IOException("Invalid protocol number"); }
    }
    static boolean bool(JsonObject value, String name) throws IOException {
        if (!value.has(name) || !value.get(name).isJsonPrimitive()
                || !value.getAsJsonPrimitive(name).isBoolean()) throw new IOException("Missing protocol boolean");
        return value.get(name).getAsBoolean();
    }
    static String identity(String mode, String username, String uuid) throws IOException {
        if (username == null || !username.matches("[A-Za-z0-9_]{1,16}")) throw new IOException("Invalid player identity");
        if ("offline".equals(mode)) return "offline:" + username.toLowerCase(Locale.ROOT);
        try {
            String canonical = UUID.fromString(uuid).toString();
            if (!canonical.equalsIgnoreCase(uuid)) throw new IllegalArgumentException();
            return "online:" + canonical;
        } catch (IllegalArgumentException | NullPointerException e) { throw new IOException("Invalid player UUID"); }
    }
    static String ip(String address) throws IOException {
        if (address == null || address.isBlank() || address.length() > 64
                || !address.matches("[0-9A-Fa-f:.]+")) throw new IOException("Invalid numeric IP address");
        try { return InetAddress.getByName(address).getHostAddress(); }
        catch (UnknownHostException e) { throw new IOException("Invalid numeric IP address"); }
    }
    static JsonObject common(Config config) {
        JsonObject value = new JsonObject();
        value.addProperty("protocol_version", VERSION);
        value.addProperty("scope", config.scope());
        value.addProperty("identity_mode", config.identityMode());
        return value;
    }
    static void response(JsonObject value, Config config) throws IOException {
        if (integer(value, "protocol_version") != VERSION || !config.scope().equals(string(value, "scope")))
            throw new IOException("Bridge scope or protocol mismatch");
    }
}
