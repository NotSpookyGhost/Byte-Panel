package app.bytedev.guard;

import java.lang.reflect.*;
import org.bukkit.entity.Player;
import org.bukkit.event.*;
import org.bukkit.plugin.Plugin;

/** Reflection avoids bundling AuthMe or linking a platform-specific AuthMe implementation. */
final class AuthMeAdapter {
    private final BytePanelGuard owner;
    private final Listener listener = new Listener() {};
    private Plugin provider;
    private Object api;
    private Method authenticated;
    private Method unrestricted;
    private boolean hooks;

    AuthMeAdapter(BytePanelGuard owner) { this.owner = owner; }

    boolean refresh() {
        try {
            Plugin candidate = owner.getServer().getPluginManager().getPlugin("AuthMe");
            if (candidate == null || !candidate.isEnabled()
                    || !candidate.getDescription().getVersion().matches("6\\.0\\.1(?:[-+].*)?")) return false;
            if (candidate != provider) {
                HandlerList.unregisterAll(listener);
                provider = candidate;
                hooks = false;
                ClassLoader loader = candidate.getClass().getClassLoader();
                Class<?> type = Class.forName("fr.xephi.authme.api.v3.AuthMeApi", true, loader);
                api = type.getMethod("getInstance").invoke(null);
                if (api == null) return false;
                authenticated = type.getMethod("isAuthenticated", Player.class);
                unrestricted = type.getMethod("isUnrestricted", Player.class);
                hook(loader, "fr.xephi.authme.events.LoginEvent", owner::authenticated);
                hook(loader, "fr.xephi.authme.events.LogoutEvent", owner::loggedOut);
                hooks = true;
            } else if (api == null) {
                provider = null;
                return false;
            }
            return hooks && api != null;
        } catch (ReflectiveOperationException | LinkageError | RuntimeException e) {
            api = null;
            return false;
        }
    }

    private void hook(ClassLoader loader, String eventName, java.util.function.Consumer<Player> callback)
            throws ReflectiveOperationException {
        Class<? extends Event> eventType = Class.forName(eventName, true, loader).asSubclass(Event.class);
        Method playerMethod = eventType.getMethod("getPlayer");
        owner.getServer().getPluginManager().registerEvent(eventType, listener, EventPriority.MONITOR,
                (ignored, event) -> {
                    if (!eventType.isInstance(event)) return;
                    try {
                        Player player = (Player) playerMethod.invoke(event);
                        if (event.isAsynchronous()) {
                            owner.getServer().getScheduler().runTask(owner, () -> callback.accept(player));
                        } else callback.accept(player);
                    } catch (ReflectiveOperationException | RuntimeException e) {
                        owner.authenticationFailed();
                    }
                }, owner);
    }

    boolean authenticated(Player player) {
        try {
            return provider != null && provider.isEnabled() && api != null
                    && Boolean.TRUE.equals(authenticated.invoke(api, player))
                    && Boolean.FALSE.equals(unrestricted.invoke(api, player));
        } catch (ReflectiveOperationException | RuntimeException e) { return false; }
    }
}
