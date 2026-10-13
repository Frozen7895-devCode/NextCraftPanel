import java.io.*;
import java.nio.file.*;
import java.time.LocalTime;
import java.time.format.DateTimeFormatter;
import java.util.*;

/**
 * Faux serveur Minecraft pour les tests automatisés de NexCraft Panel.
 * Imite le format des journaux et quelques commandes ; ne lance aucun vrai serveur.
 * Options : --ignore-stop (ignore la commande stop), --slow-start (démarrage plus lent).
 */
public class FakeMinecraftServer {
    static final DateTimeFormatter F = DateTimeFormatter.ofPattern("HH:mm:ss");
    static final Set<String> players = new TreeSet<>();

    static void log(String level, String msg) {
        String line = "[" + LocalTime.now().format(F) + "] [Server thread/" + level + "]: " + msg;
        System.out.println(line);
        System.out.flush();
    }

    public static void main(String[] args) throws Exception {
        boolean ignoreStop = Arrays.asList(args).contains("--ignore-stop");
        boolean slow = Arrays.asList(args).contains("--slow-start");
        System.out.println("[" + LocalTime.now().format(F) + "] [main/INFO]: Starting minecraft server version 1.21.1");
        Path props = Paths.get("server.properties");
        if (!Files.exists(props)) {
            Files.writeString(props, "server-port=25565\nmax-players=20\nlevel-name=world\nmotd=Fake server\n");
        }
        Path world = Paths.get("world");
        if (!Files.exists(world.resolve("level.dat"))) {
            Files.createDirectories(world.resolve("region"));
            Files.write(world.resolve("level.dat"), new byte[]{1, 2, 3, 4});
            Files.write(world.resolve("region/r.0.0.mca"), new byte[4096]);
        }
        log("INFO", "Loading properties");
        log("WARN", "Fake server : ne pas utiliser en production");
        Thread.sleep(slow ? 3000 : 600);
        log("INFO", "Done (0.6s)! For help, type \"help\"");
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in, "UTF-8"));
        String line;
        while ((line = in.readLine()) != null) {
            line = line.trim();
            if (line.isEmpty()) continue;
            String[] p = line.split("\\s+", 2);
            switch (p[0]) {
                case "stop":
                    if (ignoreStop) { log("INFO", "stop ignoré (--ignore-stop)"); break; }
                    log("INFO", "Stopping the server");
                    log("INFO", "Saving worlds");
                    System.exit(0);
                    break;
                case "list":
                    log("INFO", "There are " + players.size() + " of a max of 20 players online: " + String.join(", ", players));
                    break;
                case "say":
                    log("INFO", "[Server] " + (p.length > 1 ? p[1] : ""));
                    break;
                case "save-off":
                    log("INFO", "Automatic saving is now disabled");
                    break;
                case "save-all":
                    log("INFO", "Saving the game (this may take a moment!)");
                    log("INFO", "Saved the game");
                    break;
                case "save-on":
                    log("INFO", "Automatic saving is now enabled");
                    break;
                case "join":
                    if (p.length > 1) {
                        players.add(p[1]);
                        log("INFO", "UUID of player " + p[1] + " is 069a79f4-44e9-4726-a5be-fca90e38aaf5");
                        log("INFO", p[1] + " joined the game");
                    }
                    break;
                case "leave":
                    if (p.length > 1) { players.remove(p[1]); log("INFO", p[1] + " left the game"); }
                    break;
                case "oom":
                    log("ERROR", "java.lang.OutOfMemoryError: Java heap space");
                    log("ERROR", "\tat net.minecraft.server.Main.main(Main.java:42)");
                    break;
                case "crash":
                    log("ERROR", "Encountered an unexpected exception");
                    log("ERROR", "java.lang.IllegalStateException: crash volontaire du faux serveur");
                    System.out.println("\tat FakeMinecraftServer.main(FakeMinecraftServer.java)");
                    System.exit(1);
                    break;
                default:
                    log("INFO", "Unknown or incomplete command, see below for error");
            }
        }
    }
}
