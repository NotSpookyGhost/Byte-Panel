package app.bytedev.guard;

import com.google.gson.JsonObject;
import java.lang.reflect.Proxy;
import java.util.concurrent.atomic.AtomicBoolean;
import org.bukkit.entity.Player;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class SessionBindingTest {
    @Test void exactPlayerObjectCannotBeReboundToConcurrentSameNamePlayer() {
        Player first = player(), second = player();
        GuardEngine.Attempt attempt = new GuardEngine.Attempt("test", "offline:alice", new JsonObject(),
                true, 0, new AtomicBoolean(), new AtomicBoolean(), new AtomicBoolean());
        BytePanelGuard.Session session = new BytePanelGuard.Session(attempt, "8.8.8.8");
        assertTrue(session.bind(first));
        assertTrue(session.bind(first));
        assertFalse(session.bind(second));
        assertSame(first, session.player.get());
    }
    private Player player() {
        return (Player) Proxy.newProxyInstance(Player.class.getClassLoader(), new Class<?>[]{Player.class},
                (proxy, method, arguments) -> method.getName().equals("getName") ? "Alice" : null);
    }
    @Test void logoutRevokesOnlyTheExactCurrentSession() {
        Player first = player(), other = player();
        GuardEngine.Attempt attempt = new GuardEngine.Attempt("test", "offline:alice", new JsonObject(),
                true, 0, new AtomicBoolean(), new AtomicBoolean(), new AtomicBoolean());
        BytePanelGuard.Session session = new BytePanelGuard.Session(attempt, "8.8.8.8");
        session.bind(first);
        session.loginEvent.set(true); session.authenticated.set(true);
        session.unauthenticate(other);
        assertTrue(session.authenticated.get());
        session.unauthenticate(first);
        assertFalse(session.authenticated.get());
        assertFalse(session.loginEvent.get());
    }
    @Test void passwordArgumentsAreNeverPartOfCommandLabel() {
        assertEquals("login", BytePanelGuard.authenticationCommandLabel("/login never-store-this"));
        assertEquals("authme:login", BytePanelGuard.authenticationCommandLabel("/AuthMe:Login secret"));
        assertEquals("", BytePanelGuard.authenticationCommandLabel(" /login secret"));
        assertEquals("", BytePanelGuard.authenticationCommandLabel("/"));
        assertEquals("", BytePanelGuard.authenticationCommandLabel(null));
    }
}
