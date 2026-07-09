"""Lista las tablas existentes en la DB del .env."""

import psycopg

from umbra.config import settings


def main() -> None:
    url = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
            ORDER BY table_name
            """
        )
        tables = [r[0] for r in cur.fetchall()]
        print("Tables:", tables)


if __name__ == "__main__":
    main()
