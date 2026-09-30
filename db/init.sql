-- 冷链追责服务 数据库初始化脚本
-- 由 PostgreSQL 官方镜像在数据卷首次创建时自动执行（/docker-entrypoint-initdb.d）。
-- 应用启动时也会执行 CREATE TABLE IF NOT EXISTS 作为兜底。

CREATE TYPE event_type AS ENUM ('measurement', 'handover');

CREATE TABLE IF NOT EXISTS cargo (
    cargo_id          VARCHAR(64)   PRIMARY KEY,
    started_at        TIMESTAMPTZ   NOT NULL,
    temp_upper_limit  NUMERIC(10,3) NOT NULL,
    initial_holder    VARCHAR(128)  NOT NULL,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS event (
    event_id     VARCHAR(64)   PRIMARY KEY,
    cargo_id     VARCHAR(64)   NOT NULL
                     REFERENCES cargo(cargo_id) ON DELETE RESTRICT,
    event_type   event_type    NOT NULL,
    occurred_at  TIMESTAMPTZ   NOT NULL,
    temperature  NUMERIC(10,3),
    giver        VARCHAR(128),
    receiver     VARCHAR(128),
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- 事件形状约束：测温只有温度；交接只有交出方/接收方且双方不同
    CONSTRAINT chk_event_shape CHECK (
        (event_type = 'measurement'
            AND temperature IS NOT NULL
            AND giver IS NULL
            AND receiver IS NULL)
        OR
        (event_type = 'handover'
            AND temperature IS NULL
            AND giver IS NOT NULL
            AND receiver IS NOT NULL
            AND giver <> receiver)
    )
);

CREATE INDEX IF NOT EXISTS ix_event_cargo_time
    ON event (cargo_id, occurred_at);

-- 同一货物同一时刻至多一笔测温
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_cargo_time_measurement
    ON event (cargo_id, occurred_at)
    WHERE event_type = 'measurement';

-- 同一货物同一时刻至多一笔交接
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_cargo_time_handover
    ON event (cargo_id, occurred_at)
    WHERE event_type = 'handover';
