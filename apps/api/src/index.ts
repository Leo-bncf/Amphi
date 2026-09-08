import { loadConfig } from "./config.js";
import { createContext } from "./context.js";
import { buildServer } from "./server.js";

const config = loadConfig();
const ctx = createContext(config);
const app = buildServer(ctx);

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, () => {
    void (async () => {
      app.log.info("arrêt demandé");
      await app.close();
      await ctx.close();
      process.exit(0);
    })();
  });
}

try {
  await app.listen({ port: config.PORT, host: config.HOST });
} catch (error) {
  app.log.error(error);
  process.exit(1);
}
