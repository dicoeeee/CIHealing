import example.DefaultRouter;
import example.Feature;
import example.Router;

public final class BehaviorProbe {
    public static void main(String[] args) {
        Router router = new DefaultRouter();
        Feature feature = new Feature(router);
        for (String payload : ProbeInputs.PAYLOADS) {
            for (String condition : ProbeInputs.CONDITIONS) {
                for (int slot : ProbeInputs.SLOTS) {
                    String expected = "base:" + payload;
                    if (!feature.apply(payload, condition, slot).equals(expected)) {
                        throw new AssertionError("preserved feature behavior");
                    }
                }
            }
        }
    }
}
