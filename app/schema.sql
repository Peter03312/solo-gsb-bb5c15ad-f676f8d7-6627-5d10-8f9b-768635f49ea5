-- 冷链追责服务 schema。应用启动时也会幂等执行本文件（IF NOT EXISTS），
-- 同时挂载到 postgres 镜像的 /docker-entrypoint-initdb.d 供首次初始化。

CREATE TABLE IF NOT EXISTS cargo (
    cargo_no         TEXT PRIMARY KEY,
    start_time       TIMESTAMPTZ      NOT NULL,
    temp_upper_limit NUMERIC(12, 4)   NOT NULL,
    initial_holder   TEXT             NOT NULL,
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,                 -- 事件ID，全局唯一（重传去重）
    cargo_no     TEXT NOT NULL REFERENCES cargo(cargo_no),
    kind         TEXT NOT NULL CHECK (kind IN ('measurement', 'handover')),
    occurred_at  TIMESTAMPTZ NOT NULL,             -- 发生时间（UTC）
    temperature  NUMERIC(12, 4),                   -- 测温事件的温度
    from_party   TEXT,                             -- 交接事件的交出方
    to_party     TEXT,                             -- 交接事件的接收方
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 类型与载荷完整性
    CONSTRAINT events_measurement_payload CHECK (
        (kind = 'measurement' AND temperature IS NOT NULL
                               AND from_party IS NULL AND to_party IS NULL)
        OR
        (kind = 'handover' AND from_party IS NOT NULL AND to_party IS NOT NULL
                            AND temperature IS NULL)
    )
);

-- 按货物 + 时间取事件（重建与查询）
CREATE INDEX IF NOT EXISTS idx_events_cargo_time
    ON events (cargo_no, occurred_at);

-- 同一货物同一时刻不得有多笔测温 / 多笔交接。
-- 部分唯一索引允许“同一时刻一笔测温 + 一笔交接”。
CREATE UNIQUE INDEX IF NOT EXISTS uq_events_cargo_time_measurement
    ON events (cargo_no, occurred_at)
    WHERE kind = 'measurement';

CREATE UNIQUE INDEX IF NOT EXISTS uq_events_cargo_time_handover
    ON events (cargo_no, occurred_at)
    WHERE kind = 'handover';
