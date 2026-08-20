import json
import psycopg2
from psycopg2.extras import Json

DB_CFG = {
    "host": "172.136.16.52",
    "port": 5432,
    "dbname": "postgres",
    "user": "postgres",
}

CAIQU_PATH = "/home/server/python/map_assistant_v1/frontend/public/data/caiqu.geojson"
HX_PATH = "/home/server/python/map_assistant_v1/frontend/public/data/hx.geojson"

DDL_POSTGIS = """
CREATE EXTENSION IF NOT EXISTS postgis;
"""

DDL_HX = """
CREATE TABLE IF NOT EXISTS "hx" (
    id SERIAL PRIMARY KEY,
    geom geometry(MULTILINESTRING, 4326),
    properties jsonb
);
CREATE INDEX IF NOT EXISTS hx_gix ON "hx" USING GIST(geom);
"""

DDL_CAIQU = """
CREATE TABLE IF NOT EXISTS "caiqu" (
    id SERIAL PRIMARY KEY,
    geom geometry(MULTIPOLYGON, 4326),
    properties jsonb
);
CREATE INDEX IF NOT EXISTS caiqu_gix ON "caiqu" USING GIST(geom);
"""

SQL_INSERT_HX = """
INSERT INTO "hx"(geom, properties)
VALUES (ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)), %s)
"""

SQL_INSERT_CAIQU = """
INSERT INTO "caiqu"(geom, properties)
VALUES (ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)), %s)
"""


def load_geojson(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def import_features(conn, table, fc):
    cur = conn.cursor()
    count = 0
    for feat in fc.get("features", []):
        geom = feat.get("geometry")
        props = feat.get("properties") or {}
        if not geom:
            continue
        gtype = (geom.get("type") or "").upper()
        if table == "hx" and "LINESTRING" not in gtype:
            continue
        if table == "caiqu" and "POLYGON" not in gtype:
            continue
        gj = json.dumps(geom, ensure_ascii=False)
        if table == "hx":
            cur.execute(SQL_INSERT_HX, (gj, Json(props)))
        else:
            cur.execute(SQL_INSERT_CAIQU, (gj, Json(props)))
        count += 1
    conn.commit()
    return count


def main():
    conn = psycopg2.connect(**DB_CFG)
    cur = conn.cursor()
    cur.execute(DDL_POSTGIS)
    cur.execute(DDL_HX)
    cur.execute(DDL_CAIQU)
    conn.commit()

    hx_fc = load_geojson(HX_PATH)
    caiqu_fc = load_geojson(CAIQU_PATH)
    hx_count = import_features(conn, "hx", hx_fc)
    caiqu_count = import_features(conn, "caiqu", caiqu_fc)
    print(f"Imported hx: {hx_count}, caiqu: {caiqu_count}")
    conn.close()


if __name__ == "__main__":
    main()
