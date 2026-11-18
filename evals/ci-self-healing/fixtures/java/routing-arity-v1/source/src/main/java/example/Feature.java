package example;

public final class Feature {
    private final Router router;

    public Feature(Router router) {
        this.router = router;
    }

    public String apply(String payload, String condition, int slot) {
        return router.route(payload, condition, slot);
    }
}
