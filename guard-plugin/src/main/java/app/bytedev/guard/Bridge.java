package app.bytedev.guard;

import com.google.gson.*;
import java.io.*;
import java.net.*;
import java.net.http.*;
import java.time.Duration;

/** Only transport errors may activate fallback; HTTP, JSON and protocol failures never do. */
interface Bridge {
    JsonObject call(String route, JsonObject body) throws IOException;

    final class TransportFailure extends IOException {
        TransportFailure() { super("Private bridge transport unavailable"); }
    }

    final class Http implements Bridge {
        private final Protocol.Config config;
        private final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(400))
                .followRedirects(HttpClient.Redirect.NEVER).proxy(new ProxySelector() {
                    @Override public java.util.List<Proxy> select(URI uri) { return java.util.List.of(Proxy.NO_PROXY); }
                    @Override public void connectFailed(URI uri, SocketAddress address, IOException error) {}
                }).build();

        Http(Protocol.Config config) { this.config = config; }

        public JsonObject call(String route, JsonObject body) throws IOException {
            HttpRequest.Builder builder = HttpRequest.newBuilder(config.endpoint().resolve(route))
                    .timeout(Duration.ofMillis(1400)).header("Authorization", "Bearer " + config.token())
                    .header("Content-Type", "application/json").header("Accept", "application/json");
            if (body == null) builder.GET();
            else builder.POST(HttpRequest.BodyPublishers.ofString(Protocol.JSON.toJson(body)));
            HttpResponse<byte[]> response;
            try {
                response = client.send(builder.build(), info -> new LimitedBodySubscriber(
                        info.statusCode() == 200 && route.equals("/v1/snapshot") ? Protocol.MAX_BODY : 32768));
            } catch (ConnectException | HttpTimeoutException e) { throw new TransportFailure(); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new IOException("Bridge request interrupted"); }
            catch (IOException e) {
                // A refusal/reset is a transport outage. Invalid/oversize response is not.
                if (e.getCause() instanceof BodyTooLarge || e instanceof BodyTooLarge) throw new IOException("Bridge response too large");
                if (e instanceof java.net.SocketException || e.getCause() instanceof java.net.SocketException
                        || (e.getMessage() != null && e.getMessage().contains("header parser received no bytes")))
                    throw new TransportFailure();
                throw new IOException("Bridge request failed");
            }
            if (response.statusCode() != 200) throw new IOException("Bridge rejected request (HTTP " + response.statusCode() + ")");
            try {
                JsonObject value = JsonParser.parseString(new String(response.body(), java.nio.charset.StandardCharsets.UTF_8))
                        .getAsJsonObject();
                Protocol.response(value, config);
                return value;
            } catch (RuntimeException e) { throw new IOException("Invalid bridge response"); }
        }
    }

    final class BodyTooLarge extends IOException {}
    final class LimitedBodySubscriber implements HttpResponse.BodySubscriber<byte[]> {
        private final int limit;
        private final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        private final java.util.concurrent.CompletableFuture<byte[]> result = new java.util.concurrent.CompletableFuture<>();
        private java.util.concurrent.Flow.Subscription subscription;
        LimitedBodySubscriber(int limit) { this.limit = limit; }
        public java.util.concurrent.CompletionStage<byte[]> getBody() { return result; }
        public void onSubscribe(java.util.concurrent.Flow.Subscription subscription) {
            this.subscription = subscription; subscription.request(1);
        }
        public void onNext(java.util.List<java.nio.ByteBuffer> values) {
            for (java.nio.ByteBuffer value : values) {
                if (bytes.size() + value.remaining() > limit) {
                    subscription.cancel(); result.completeExceptionally(new BodyTooLarge()); return;
                }
                byte[] part = new byte[value.remaining()]; value.get(part); bytes.writeBytes(part);
            }
            subscription.request(1);
        }
        public void onError(Throwable error) { result.completeExceptionally(error); }
        public void onComplete() { result.complete(bytes.toByteArray()); }
    }
}
