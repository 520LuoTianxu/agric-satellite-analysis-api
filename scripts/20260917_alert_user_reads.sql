-- 预警个人已读。请先执行本脚本，再部署接口和前端；应用启动不执行 DDL。
-- base_id 与 land_parcels.base_id 一致，user_id 来自农业服务 /getInfo 的 user.userId。
BEGIN;

CREATE TABLE IF NOT EXISTS agric_satellite.alert_reads (
    base_id varchar(64) NOT NULL,
    user_id varchar(128) NOT NULL,
    alert_id uuid NOT NULL REFERENCES agric_satellite.alerts(id) ON DELETE CASCADE,
    read_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (base_id, user_id, alert_id)
);

CREATE INDEX IF NOT EXISTS ix_alert_reads_alert_id
    ON agric_satellite.alert_reads (alert_id);
CREATE INDEX IF NOT EXISTS ix_land_parcels_base_id_active
    ON agric_satellite.land_parcels (base_id) WHERE deleted_at IS NULL;

COMMENT ON TABLE agric_satellite.alert_reads IS '租户内用户独立已读；无记录表示未读';
COMMENT ON COLUMN agric_satellite.alert_reads.base_id IS '租户基地ID，对应 land_parcels.base_id';
COMMENT ON COLUMN agric_satellite.alert_reads.user_id IS '农业服务验证后的用户ID，不是账号角色ID或token';
COMMENT ON COLUMN agric_satellite.alert_reads.read_at IS '首次标记已读时间，重复请求不覆盖';

COMMIT;
