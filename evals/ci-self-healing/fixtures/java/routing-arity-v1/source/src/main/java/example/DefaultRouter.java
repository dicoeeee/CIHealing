package example;

public final class DefaultRouter implements Router {
    public String route(String payload) {
        return "base:" + payload;
    }
}
