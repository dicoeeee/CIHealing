import example.Router;
import java.lang.reflect.Method;

public final class InterfaceProbe {
    public static void main(String[] args) {
        for (Method method : Router.class.getMethods()) {
            if (method.getName().equals("route") && method.getParameterCount() > 1) {
                throw new AssertionError("preserve contract forbids additional route parameters");
            }
        }
    }
}
