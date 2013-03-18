import java.util.TreeSet;
import java.net.URLDecoder;
import java.io.File;
import java.util.jar.JarFile;
import java.util.zip.ZipEntry;
import java.io.InputStream;
import java.io.BufferedInputStream;
import java.io.OutputStream;
import java.io.BufferedOutputStream;
import java.io.FileOutputStream;

public class JarLoader
{
    static TreeSet<String> m_libCache = new TreeSet<String>();
    
    public static void load(String p_name)
    {
        synchronized(m_libCache)
        {
            if(m_libCache.contains(p_name))
            {
                return;
            }
        }
        
        String l_jarFile = "";
        
        try
        {
            String l_uri = Class.forName("JarLoader", true, ClassLoader.getSystemClassLoader()).getProtectionDomain().getCodeSource().getLocation().toURI().getPath().toString();
            l_jarFile = URLDecoder.decode(l_uri, "UTF-8");
            
            System.out.println("Opening jar file " + l_jarFile);
            JarFile l_jar = new JarFile(l_jarFile);
            ZipEntry l_entry = l_jar.getEntry("jnilibs/" + p_name);
            
            File l_tmpFile = File.createTempFile(p_name, null);
            l_tmpFile.deleteOnExit();
            
            InputStream l_in = new BufferedInputStream(l_jar.getInputStream(l_entry));
            OutputStream l_out = new BufferedOutputStream(new FileOutputStream(l_tmpFile));
            byte[] l_buffer = new byte[2048];
            for(;;)
            {
                int l_num = l_in.read(l_buffer);
                if (l_num <= 0)
                {
                    break;
                }
                l_out.write(l_buffer, 0, l_num);
            }
            l_out.flush();
            l_out.close();
            l_in.close();
            
            System.load(l_tmpFile.getCanonicalPath());

            synchronized(m_libCache)
            {
                m_libCache.add(p_name);
            }
        }
        catch (Exception e) {
            throw new UnsatisfiedLinkError("Failed to load " + p_name + " from jar file " + l_jarFile + ": " + e.getMessage());
        }
    }
}
