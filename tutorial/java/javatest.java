public class javatest
{
    static {
        JarLoader.load("tutorial.jnilib");
    }
    
    public static native void print_revision_info();
    
    public static void main(String[] p_args)
    {
        System.out.println("In Java test program");
        print_revision_info();
    }
}
