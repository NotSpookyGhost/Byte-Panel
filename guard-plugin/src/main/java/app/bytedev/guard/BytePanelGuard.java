package app.bytedev.guard;

import java.io.IOException;
import java.net.InetAddress;
import java.nio.file.Path;
import java.time.Clock;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import org.bukkit.entity.Player;
import org.bukkit.event.*;
import org.bukkit.event.player.*;
import org.bukkit.event.block.*;
import org.bukkit.event.entity.*;
import org.bukkit.event.inventory.*;
import org.bukkit.event.server.PluginDisableEvent;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * Minecraft-facing adapter. Tick-thread handlers only touch bounded in-memory state;
 * HTTP and durable storage always run on bounded workers.
 */
public final class BytePanelGuard extends JavaPlugin implements Listener {
    private static final String UNAVAILABLE = "Connection security is unavailable. Please try again shortly.";
    private final ThreadPoolExecutor checks = pool(8, 16, "bytepanel-guard-check");
    private final ThreadPoolExecutor outcomes = pool(1, 4096, "bytepanel-guard-events");
    private final ScheduledExecutorService maintenance = Executors.newSingleThreadScheduledExecutor(daemon("bytepanel-guard-maintenance"));
    private final ConcurrentMap<UUID, Session> sessions = new ConcurrentHashMap<>();
    private final ConcurrentMap<AsyncPlayerPreLoginEvent, Session> eventSessions = new ConcurrentHashMap<>();
    private volatile GuardEngine engine;
    private volatile boolean inert;
    private volatile boolean authReady;
    private volatile boolean stopping;
    private AuthMeAdapter auth;
    private Protocol.Config config;
    private SensitiveCommandFilter commandFilter;

    static final class Session {
        final GuardEngine.Attempt attempt;
        final String ip;
        final long expires;
        final AtomicReference<Player> player = new AtomicReference<>();
        final AtomicBoolean loginEvent = new AtomicBoolean();
        final AtomicBoolean joined = new AtomicBoolean();
        final AtomicBoolean authenticated = new AtomicBoolean();
        Session(GuardEngine.Attempt attempt, String ip) {
            this.attempt = attempt; this.ip = ip;
            expires = System.currentTimeMillis() + 120000;
        }
        boolean bind(Player value) {
            return player.compareAndSet(null, value) || player.get() == value;
        }
        void unauthenticate(Player value) {
            if (player.get() != value) return;
            loginEvent.set(false);
            authenticated.set(false);
        }
    }
    private static ThreadFactory daemon(String name) {
        AtomicInteger count = new AtomicInteger();
        return runnable -> { Thread thread = new Thread(runnable, name + "-" + count.incrementAndGet()); thread.setDaemon(true); return thread; };
    }
    private static ThreadPoolExecutor pool(int threads, int queue, String name) {
        return new ThreadPoolExecutor(threads, threads, 0, TimeUnit.MILLISECONDS,
                new ArrayBlockingQueue<>(queue), daemon(name), new ThreadPoolExecutor.AbortPolicy());
    }

    @Override public void onEnable() {
        try {
            commandFilter = new SensitiveCommandFilter();
            commandFilter.install();
        } catch (LinkageError | RuntimeException e) {
            getLogger().warning("Source command-log filter is unavailable; keep AuthMe sensitive logging disabled and panel redaction enabled.");
        }
        getServer().getPluginManager().registerEvents(this, this);
        auth = new AuthMeAdapter(this);
        // Register restrictions first: invalid configuration must fail closed, not disable this listener.
        maintenance.execute(() -> {
            try {
                String file = System.getProperty("bytepanel.guard.config", "");
                if (file.isBlank()) throw new IOException("Private configuration path missing");
                config = Protocol.Config.load(Path.of(file), Path.of("."));
                if (config.mode().equals("off")) {
                    inert = true;
                    return;
                }
                if (getServer().getOnlineMode() != config.identityMode().equals("online"))
                    throw new IOException("Effective online-mode does not match configured identity mode");
                engine = new GuardEngine(config, new Bridge.Http(config), new DurableState(config), Clock.systemUTC());
            } catch (IOException | RuntimeException e) {
                getLogger().severe("Byte Guard cannot initialize private state; new connections will be refused. Check panel Security health.");
            }
        });
        maintenance.scheduleWithFixedDelay(() -> {
            GuardEngine active = engine;
            if (active != null && !stopping) active.maintain(authReady);
        }, 0, 5, TimeUnit.SECONDS);
        getServer().getScheduler().runTaskTimer(this, () -> {
            if (inert) return;
            authReady = auth.refresh();
            GuardEngine active = engine;
            if (active == null) return;
            for (Map.Entry<UUID, Session> entry : sessions.entrySet()) {
                Session session = entry.getValue();
                Player player = session.player.get();
                if (player == null && session.expires < System.currentTimeMillis()) {
                    if (sessions.remove(entry.getKey(), session)) queueOutcome(session, "disconnected");
                } else if (active.offlineRequired() && player != null) {
                    if (authReady && session.authenticated.get() && !auth.authenticated(player))
                        session.unauthenticate(player);
                    if (!session.authenticated.get()) {
                        if (!authReady) player.kickPlayer(UNAVAILABLE);
                        else if (session.loginEvent.get() && auth.authenticated(player)) finalizeAuthentication(session, player);
                    }
                }
            }
        }, 1, 20);
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void prelogin(AsyncPlayerPreLoginEvent event) {
        if (inert) return;
        if (event.getLoginResult() != AsyncPlayerPreLoginEvent.Result.ALLOWED) return;
        GuardEngine active = engine;
        if (active == null || stopping) { event.disallow(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, UNAVAILABLE); return; }
        AtomicBoolean cancelled = new AtomicBoolean();
        Future<GuardEngine.Decision> future;
        try {
            future = checks.submit(() -> active.check(event.getName(), event.getUniqueId().toString(),
                    event.getAddress().getHostAddress(), cancelled, authReady));
        } catch (RejectedExecutionException e) {
            event.disallow(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, "Connection checks are busy. Please retry."); return;
        }
        try {
            GuardEngine.Decision decision = future.get(1950, TimeUnit.MILLISECONDS);
            if (!decision.allowed()) {
                event.disallow(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, decision.message()); return;
            }
            Session session = new Session(decision.attempt(), Protocol.ip(event.getAddress().getHostAddress()));
            if (sessions.putIfAbsent(event.getUniqueId(), session) != null) {
                queueOutcome(session, "disconnected");
                event.disallow(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, "This account already has a connection in progress.");
            } else eventSessions.put(event, session);
        } catch (TimeoutException | InterruptedException | ExecutionException | IOException e) {
            cancelled.set(true);
            // Do not interrupt a durable write; the worker observes cancellation and records disconnect.
            event.disallow(AsyncPlayerPreLoginEvent.Result.KICK_OTHER, "Connection check timed out. Please retry.");
            if (e instanceof InterruptedException) Thread.currentThread().interrupt();
        }
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void afterPrelogin(AsyncPlayerPreLoginEvent event) {
        Session owned = eventSessions.remove(event);
        if (event.getLoginResult() == AsyncPlayerPreLoginEvent.Result.ALLOWED) return;
        // Only release the reservation created by this exact event, never a rejected duplicate.
        if (owned != null && sessions.remove(event.getUniqueId(), owned)) queueOutcome(owned, "disconnected");
    }

    @EventHandler(priority = EventPriority.HIGHEST)
    public void login(PlayerLoginEvent event) {
        if (inert) return;
        if (event.getResult() != PlayerLoginEvent.Result.ALLOWED) return;
        Session session = sessions.get(event.getPlayer().getUniqueId());
        GuardEngine active = engine;
        if (session == null || active == null || (active.offlineRequired() && !authReady)
                || !sameIp(session, event.getAddress()) || session.expires < System.currentTimeMillis()
                || !session.bind(event.getPlayer())) event.disallow(PlayerLoginEvent.Result.KICK_OTHER, UNAVAILABLE);
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void afterLogin(PlayerLoginEvent event) {
        if (event.getResult() == PlayerLoginEvent.Result.ALLOWED) return;
        Session session = sessions.get(event.getPlayer().getUniqueId());
        if (session != null && session.player.get() == event.getPlayer()
                && sessions.remove(event.getPlayer().getUniqueId(), session)) queueOutcome(session, "disconnected");
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void join(PlayerJoinEvent event) {
        if (inert) return;
        Player player = event.getPlayer();
        Session session = sessions.get(player.getUniqueId());
        if (session == null || session.player.get() != player || engine == null) {
            player.kickPlayer(UNAVAILABLE); return;
        }
        session.joined.set(true);
        if (!queueOutcome(session, "joined")) { player.kickPlayer(UNAVAILABLE); return; }
        // Only a successful AuthMe LoginEvent for this exact Player can finalize offline trust.
        // API state alone is username keyed and could otherwise refer to a previous connection.
        if (engine.offlineRequired() && session.loginEvent.get() && auth.authenticated(player))
            finalizeAuthentication(session, player);
    }

    void authenticated(Player player) {
        GuardEngine active = engine;
        if (active == null || !active.offlineRequired() || !authReady || !auth.authenticated(player)) return;
        Session session = sessions.get(player.getUniqueId());
        if (session == null || player.getAddress() == null
                || !sameIp(session, player.getAddress().getAddress()) || !session.bind(player)) return;
        session.loginEvent.set(true);
        if (session.joined.get()) finalizeAuthentication(session, player);
    }
    void loggedOut(Player player) {
        Session session = sessions.get(player.getUniqueId());
        if (session != null) session.unauthenticate(player);
    }

    private void finalizeAuthentication(Session session, Player player) {
        if (session.player.get() != player || !session.joined.get() || !session.loginEvent.get()
                || !auth.authenticated(player) || !session.authenticated.compareAndSet(false, true)) return;
        if (!queueOutcome(session, "authenticated")) {
            session.authenticated.set(false); player.kickPlayer(UNAVAILABLE);
        }
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void quit(PlayerQuitEvent event) {
        Session session = sessions.get(event.getPlayer().getUniqueId());
        if (session != null && session.player.get() == event.getPlayer()
                && sessions.remove(event.getPlayer().getUniqueId(), session)) queueOutcome(session, "disconnected");
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void providerDisabled(PluginDisableEvent event) {
        if (event.getPlugin().getName().equals("AuthMe")) authenticationFailed();
    }
    void authenticationFailed() {
        authReady = false;
        GuardEngine active = engine;
        if (active == null || !active.offlineRequired()) return;
        for (Session session : sessions.values()) {
            Player player = session.player.get();
            if (player != null && !session.authenticated.get()) player.kickPlayer(UNAVAILABLE);
        }
    }

    private static boolean sameIp(Session session, InetAddress address) {
        try { return address != null && session.ip.equals(Protocol.ip(address.getHostAddress())); }
        catch (IOException e) { return false; }
    }

    private boolean queueOutcome(Session session, String stage) {
        GuardEngine active = engine;
        if (active == null) return false;
        try {
            outcomes.execute(() -> {
                try { active.outcome(session.attempt.id(), stage); }
                catch (IOException | RuntimeException e) {
                    getLogger().severe("Byte Guard could not persist a connection outcome; new connections are paused.");
                    Player player = session.player.get();
                    if (!stopping && player != null)
                        getServer().getScheduler().runTask(this, () -> {
                            if (sessions.get(player.getUniqueId()) == session) player.kickPlayer(UNAVAILABLE);
                        });
                }
            });
            return true;
        } catch (RejectedExecutionException e) { return false; }
    }

    private boolean unauthenticated(Player player) {
        GuardEngine active = engine;
        if (inert || active == null || !active.offlineRequired()) return false;
        Session session = sessions.get(player.getUniqueId());
        return session == null || session.player.get() != player || !session.authenticated.get();
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictMovement(PlayerMoveEvent event) {
        if (unauthenticated(event.getPlayer()) && event.getTo() != null
                && (event.getFrom().getX() != event.getTo().getX()
                || event.getFrom().getY() != event.getTo().getY()
                || event.getFrom().getZ() != event.getTo().getZ())) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictInteraction(PlayerInteractEvent event) {
        if (unauthenticated(event.getPlayer())) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictEntityInteraction(PlayerInteractEntityEvent event) {
        if (unauthenticated(event.getPlayer())) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictBreak(BlockBreakEvent event) {
        if (unauthenticated(event.getPlayer())) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictPlace(BlockPlaceEvent event) {
        if (unauthenticated(event.getPlayer())) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictDrop(PlayerDropItemEvent event) {
        if (unauthenticated(event.getPlayer())) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictPickup(EntityPickupItemEvent event) {
        if (event.getEntity() instanceof Player player && unauthenticated(player)) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictInventory(InventoryClickEvent event) {
        if (event.getWhoClicked() instanceof Player player && unauthenticated(player)) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictInventoryDrag(InventoryDragEvent event) {
        if (event.getWhoClicked() instanceof Player player && unauthenticated(player)) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictInventoryOpen(InventoryOpenEvent event) {
        if (event.getPlayer() instanceof Player player && unauthenticated(player)) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictDamage(EntityDamageEvent event) {
        if (event.getEntity() instanceof Player player && unauthenticated(player)) event.setCancelled(true);
        if (event instanceof EntityDamageByEntityEvent attack
                && attack.getDamager() instanceof Player player && unauthenticated(player)) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictChat(AsyncPlayerChatEvent event) {
        if (unauthenticated(event.getPlayer())) event.setCancelled(true);
    }
    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void restrictCommands(PlayerCommandPreprocessEvent event) {
        if (!unauthenticated(event.getPlayer())) return;
        event.setCancelled(true);
        // Inspect only the command label; never copy, transmit or log its argument/password.
        String label = authenticationCommandLabel(event.getMessage());
        if (label.isEmpty()) return;
        org.bukkit.command.PluginCommand command = getServer().getPluginCommand(label);
        if (command != null && command.getPlugin().getName().equals("AuthMe")
                && Set.of("login", "register", "email", "totp", "captcha", "verification").contains(command.getName()))
            event.setCancelled(false);
    }
    static String authenticationCommandLabel(String message) {
        if (message == null || message.length() < 2 || message.charAt(0) != '/') return "";
        int end = message.indexOf(' ');
        String label = message.substring(1, end < 0 ? message.length() : end).toLowerCase(Locale.ROOT);
        return label.matches("(?:authme:)?[a-z0-9]+") ? label : "";
    }

    @Override public void onDisable() {
        stopping = true;
        checks.shutdown();
        maintenance.shutdown();
        for (Session session : sessions.values()) queueOutcome(session, "disconnected");
        outcomes.shutdown();
        if (commandFilter != null) commandFilter.remove();
        // No filesystem/network wait on the tick thread. Workers finish queued durable writes.
    }
}
