# cdfinance groupSiteAdmission 现场准入问卷

## 上游

- `GET https://joint-venture-test.cdfinance.com.cn/agric-api/agriculture/groupSiteAdmission/{groupId}`
- 生产：将 base 换成 `https://joint-venture.cdfinance.com.cn/agric-api`（与 NPK 共用 `CDFINANCE_SOIL_BASE_URL`）
- Headers：`Authorization: Bearer <JWT>`、`channel-net: H5`、`hr-base-id`、`x-cfpamf-app-key`、`content-type: application/x-www-form-urlencoded`
- 签名 query（`timestamp/nonce/z_seller/sv/sign`）可选；JWT + headers 通常即可

## 响应要点（`data`）

- 顶层：`groupId`、`score`、`status`、`totalArea`、`avgYield`、`muProfit`、`scoreView`、`payload`
- `payload.answers.assessmentScope.groups[].itemAnswers`：土壤类型/水源/排水等选项文案
- `payload.answers.redLineAnswers`、`plannedCrops`
- `scoreView.groups[].dimensions[].items`：维度得分与选项

## 存储

表 `group_site_admission`：按 `group_id` 唯一；可选 `field_id` / `land_id`；`summary_json` + `vendor_payload`。

## groupId ↔ 田块

1. 请求体显式 `group_id`
2. 字段 tags：`cdfinance_group:<id>` 或 `group:<id>`（拉取成功后可写入）
3. `agri:<land_id>` → `agric_satellite.land_parcels.group_id`

## API

- `GET /v1/fields/{id}/site-admission`
- `POST /v1/fields/{id}/site-admission` — Bearer 同 NPK；`force=true` 刷新
- Assessment / season-growth generate bodies: optional `cdfinance_token` + `group_id` (soft prefetch before MQ)

## 评估

`load_site_admission` → `facts_for_llm.site_admission`；PDF「地块基础画像」卡片 + 管理建议要点。软缺失 OK。

## Report generate (选地体检 / 生育期长势)

`POST .../assessment-report` and `POST .../season-growth-report` accept optional:

- `cdfinance_token` (alias `token`) — temporary H5 Bearer
- `group_id` — questionnaire groupId
- `hr_base_id` — optional override for `hr-base-id` header (falls back to `CDFINANCE_HR_BASE_ID`)

When token is present, API host **soft-prefetches** (never blocks PDF):

1. site admission upsert → `group_site_admission` (needs resolvable groupId)
2. NPK upsert → `soil_nutrient_npk`

Bearer is **not** stored in job params / MQ. PDF workers keep reading from DB.
