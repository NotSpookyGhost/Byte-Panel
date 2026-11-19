package app.bytedev.guard;

import java.util.*;
import java.util.regex.Pattern;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.core.*;
import org.apache.logging.log4j.core.config.LoggerConfig;
import org.apache.logging.log4j.core.filter.AbstractFilter;

/** Suppress vanilla command-audit lines containing authentication secrets before appenders. */
final class SensitiveCommandFilter extends AbstractFilter {
    private static final Pattern SECRET = Pattern.compile(
            "(?i)(?:issued server command:\\s*/|executing(?: server)? command\\s*[:>]\\s*/?)"
            + "(?:[a-z0-9_]+:)?(?:login|l|log|register|reg|changepassword|changepass|cp|"
            + "unregister|unreg|email|totp|2fa|captcha|verification)(?:\\s|$)");
    private static final Pattern ADMIN = Pattern.compile(
            "(?i)(?:issued server command:\\s*/|executing(?: server)? command\\s*[:>]\\s*/?)"
            + "(?:authme:)?authme\\s+(?:register|password|changepassword)\\s");
    private final Set<LoggerConfig> installed = new HashSet<>();

    static boolean sensitive(String message) {
        return message != null && (SECRET.matcher(message).find() || ADMIN.matcher(message).find());
    }
    void install() {
        if (!(LogManager.getContext(false) instanceof LoggerContext context)) return;
        installed.add(context.getConfiguration().getRootLogger());
        installed.addAll(context.getConfiguration().getLoggers().values());
        start();
        for (LoggerConfig logger : installed) logger.addFilter(this);
        context.updateLoggers();
    }
    void remove() {
        for (LoggerConfig logger : installed) logger.removeFilter(this);
        installed.clear();
        stop();
    }
    @Override public Result filter(LogEvent event) {
        return event != null && event.getMessage() != null && sensitive(event.getMessage().getFormattedMessage())
                ? Result.DENY : Result.NEUTRAL;
    }
}
