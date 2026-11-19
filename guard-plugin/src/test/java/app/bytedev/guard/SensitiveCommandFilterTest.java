package app.bytedev.guard;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class SensitiveCommandFilterTest {
    @Test void authenticationAliasesAreSuppressed() {
        for (String command : new String[]{"login", "l", "log", "register", "reg", "changepassword",
                "changepass", "cp", "authme:login", "email", "totp", "2fa", "captcha", "verification"}) {
            assertTrue(SensitiveCommandFilter.sensitive("Alice issued server command: /" + command + " example-secret"), command);
        }
        assertTrue(SensitiveCommandFilter.sensitive("Admin issued server command: /authme register Alice example-secret"));
    }
    @Test void ordinaryCommandsAndStatusDoNotMatch() {
        assertFalse(SensitiveCommandFilter.sensitive("Alice issued server command: /plugins"));
        assertFalse(SensitiveCommandFilter.sensitive("Alice issued server command: /help login"));
        assertFalse(SensitiveCommandFilter.sensitive("Registered player Alice successfully"));
        assertFalse(SensitiveCommandFilter.sensitive(null));
    }
}
