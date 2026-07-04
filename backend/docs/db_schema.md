# 数据库结构文档 (自动生成)

当前数据库公开表结构如下：

表名: ceshen
字段:
  - ID (integer)
  - Year (integer)
  - Mineable_Area_Name (character varying)
  - County_District (character varying)
  - Measured_Depth (real)
  - Control_Elevation (real)
  - Mineable_Area_ID (character varying)
  - Lon_4326 (real)
  - Lat_4326 (real)
  - geom (USER-DEFINED)

表名: knowledge_base
字段:
  - id (integer)
  - title (text)
  - content (text)
  - source (text)
  - tags (ARRAY)
  - created_at (timestamp with time zone)
  - embedding (USER-DEFINED)
  - parent_id (integer)
  - is_child (boolean)

表名: spatial_ref_sys
字段:
  - srid (integer)
  - auth_name (character varying)
  - auth_srid (integer)
  - srtext (character varying)
  - proj4text (character varying)

表名: us_gaz
字段:
  - id (integer)
  - seq (integer)
  - word (text)
  - stdword (text)
  - token (integer)
  - is_custom (boolean)

表名: us_lex
字段:
  - id (integer)
  - seq (integer)
  - word (text)
  - stdword (text)
  - token (integer)
  - is_custom (boolean)

表名: us_rules
字段:
  - id (integer)
  - rule (text)
  - is_custom (boolean)
