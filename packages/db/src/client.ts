import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import { schema } from "./schema.js";

export type Database = ReturnType<typeof createDatabase>;

export interface DatabaseConfig {
  readonly url: string;
  /** La VM tourne à côté d'une prod qui sert un client payant : on ne monopolise pas. */
  readonly maxConnections?: number;
}

export function createDatabase(config: DatabaseConfig) {
  const client = postgres(config.url, {
    max: config.maxConnections ?? 10,
    // Les timestamps reviennent en Date, pas en chaîne : le domaine manipule des dates.
    types: {},
  });
  return drizzle(client, { schema });
}
