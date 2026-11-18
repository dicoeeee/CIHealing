import example.DefaultRouter;
import example.Router;

public final class CompatibilityProbe {
    public static void main(String[] args) {
        Router router = new DefaultRouter();
        for (String payload : ProbeInputs.PAYLOADS) {
            if (!router.route(payload).equals("base:" + payload)) {
                throw new AssertionError("legacy interface contract");
            }
        }
    }
}
